from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.knowledge_graph import (  # noqa: E402
    KnowledgeGraphDataError,
    build_ncs_knowledge_graph,
)


SCHEMA = """
CREATE TABLE classifications (
  classification_id INTEGER PRIMARY KEY,
  major_code TEXT, major_name TEXT,
  middle_code TEXT, middle_name TEXT,
  small_code TEXT, small_name TEXT,
  sub_code TEXT, sub_name TEXT
);
CREATE TABLE competency_units (
  unit_code TEXT PRIMARY KEY,
  unit_name_raw TEXT,
  unit_name_refined TEXT,
  unit_level_raw TEXT,
  api_unit_level TEXT,
  classification_id INTEGER,
  review_status TEXT
);
CREATE TABLE competency_elements (
  element_id INTEGER PRIMARY KEY,
  unit_code TEXT,
  element_no TEXT,
  element_code_raw TEXT,
  element_name_raw TEXT,
  element_name_refined TEXT,
  element_level_raw TEXT,
  api_element_level TEXT,
  review_status TEXT
);
CREATE TABLE performance_criteria (
  criteria_id INTEGER PRIMARY KEY,
  element_id INTEGER,
  criteria_no TEXT,
  criteria_text_raw TEXT,
  criteria_text_refined TEXT,
  review_status TEXT
);
CREATE TABLE ontology_concepts (
  concept_id INTEGER PRIMARY KEY,
  concept_name TEXT,
  concept_type TEXT,
  definition TEXT,
  definition_status TEXT,
  review_status TEXT
);
CREATE TABLE criteria_concept_links (
  link_id INTEGER PRIMARY KEY,
  criteria_id INTEGER,
  concept_id INTEGER,
  relation_type TEXT,
  link_status TEXT
);
CREATE TABLE ontology_concept_relations (
  relation_id INTEGER PRIMARY KEY,
  source_concept_id INTEGER,
  target_concept_id INTEGER,
  relation_type TEXT,
  relation_label TEXT,
  review_status TEXT
);
CREATE TABLE task_ksa_concept_relations (
  relation_id INTEGER PRIMARY KEY,
  criteria_id INTEGER,
  source_concept_id INTEGER,
  target_concept_id INTEGER,
  relation_type TEXT,
  confidence_score REAL,
  review_status TEXT
);
CREATE TABLE ncs_training_courses (
  training_course_id INTEGER PRIMARY KEY,
  compe_unit_name TEXT,
  compe_unit_level TEXT,
  train_goal TEXT,
  train_time TEXT,
  fac_name TEXT,
  meth_name TEXT
);
CREATE TABLE ncs_training_course_unit_links (
  link_id INTEGER PRIMARY KEY,
  training_course_id INTEGER,
  unit_code TEXT,
  link_method TEXT,
  confidence_score REAL,
  review_status TEXT
);
CREATE TABLE ncs_training_course_concept_links (
  link_id INTEGER PRIMARY KEY,
  training_course_id INTEGER,
  concept_id INTEGER,
  link_method TEXT,
  confidence_score REAL,
  evidence_text TEXT,
  review_status TEXT
);
CREATE TABLE ncs_qualification_items (
  jm_cd TEXT PRIMARY KEY,
  jm_nm TEXT,
  exam_insti_nm TEXT
);
CREATE TABLE ncs_unit_qualification_links (
  link_id INTEGER PRIMARY KEY,
  unit_code TEXT,
  jm_cd TEXT,
  link_method TEXT,
  confidence_score REAL,
  review_status TEXT
);
CREATE TABLE ncs_job_base_competencies (
  job_base_competency_id INTEGER PRIMARY KEY,
  competency_name TEXT
);
CREATE TABLE ncs_unit_job_base_links (
  link_id INTEGER PRIMARY KEY,
  unit_code TEXT,
  job_base_competency_id INTEGER,
  link_method TEXT,
  confidence_score REAL,
  review_status TEXT
);
"""


class KnowledgeGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "ncs-test.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO classifications VALUES (1, '02', '경영·회계·사무', '02', '총무·인사', '01', '인사', '01', '인사기획')"
        )
        conn.execute(
            "INSERT INTO competency_units VALUES ('U1', '인사기획', NULL, '5', NULL, 1, 'raw')"
        )
        conn.executemany(
            "INSERT INTO competency_elements VALUES (?, 'U1', ?, ?, ?, NULL, '5', NULL, 'raw')",
            [
                (11, '1', 'E1', '인사전략 수립'),
                (12, '2', 'E2', '인력운영 계획'),
            ],
        )
        conn.executemany(
            "INSERT INTO performance_criteria VALUES (?, ?, ?, ?, NULL, 'raw')",
            [
                (101, 11, '1.1', '조직의 경영전략을 분석할 수 있다.'),
                (102, 12, '2.1', '인력 수요를 산정할 수 있다.'),
            ],
        )
        conn.executemany(
            "INSERT INTO ontology_concepts VALUES (?, ?, ?, ?, ?, ?)",
            [
                (201, '경영전략 분석', 'knowledge', '자동 boilerplate 정의', 'defined', 'raw'),
                (202, '인력 수요 산정', 'skill', '사람이 검토한 산정 역량 정의', 'defined', 'human_reviewed'),
                (203, '객관적 판단', 'attitude', None, 'missing', 'raw'),
            ],
        )
        conn.executemany(
            "INSERT INTO criteria_concept_links VALUES (?, ?, ?, 'related', 'raw')",
            [
                (301, 101, 201),
                (302, 101, 203),
                (303, 102, 202),
            ],
        )
        conn.execute(
            "INSERT INTO ontology_concept_relations VALUES (401, 201, 202, 'knowledge_enables_skill', '지식이 기술을 지원', 'candidate')"
        )
        conn.execute(
            "INSERT INTO task_ksa_concept_relations VALUES (402, 101, 201, 203, 'knowledge_informs_attitude', 0.72, 'candidate')"
        )
        conn.execute(
            "INSERT INTO ncs_training_courses VALUES (501, '인사기획 실무', '5', '인사전략과 인력계획을 수립한다.', '24', '강의실', '강의 및 실습')"
        )
        conn.execute(
            "INSERT INTO ncs_training_course_unit_links VALUES (601, 501, 'U1', 'ncs_cl_cd_exact', 1.0, 'auto_linked')"
        )
        conn.execute(
            "INSERT INTO ncs_training_course_concept_links VALUES (602, 501, 202, 'training_goal_concept_text', 0.91, '훈련목표 직접 근거', 'auto_linked')"
        )
        conn.execute("INSERT INTO ncs_qualification_items VALUES ('Q1', '인사관리사', '시험기관')")
        conn.execute(
            "INSERT INTO ncs_unit_qualification_links VALUES (701, 'U1', 'Q1', 'ncs_cl_cd_exact', 1.0, 'auto_linked')"
        )
        conn.execute("INSERT INTO ncs_job_base_competencies VALUES (801, '의사소통능력')")
        conn.execute(
            "INSERT INTO ncs_unit_job_base_links VALUES (802, 'U1', 801, 'ncs_cl_cd_exact', 1.0, 'auto_linked')"
        )
        conn.commit()
        conn.close()

    def test_builds_read_only_ncs_evidence_graph(self) -> None:
        before = self.db_path.stat().st_mtime_ns
        payload = build_ncs_knowledge_graph(self.db_path, unit_code="U1")
        after = self.db_path.stat().st_mtime_ns

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema"], "ncs_knowledge_graph_v1")
        self.assertTrue(payload["read_only"])
        self.assertFalse(payload["db_writes"])
        self.assertFalse(payload["approval_claim"])
        self.assertEqual(before, after)
        self.assertFalse(payload["provenance_policy"]["external_content_used"])

        node_types = {node["type"] for node in payload["nodes"]}
        self.assertTrue(
            {
                "scope",
                "unit",
                "element",
                "task",
                "knowledge",
                "skill",
                "attitude",
                "course",
                "qualification",
                "job_base",
            }.issubset(node_types)
        )
        edge_types = {edge["type"] for edge in payload["edges"]}
        self.assertIn("task_requires_concept", edge_types)
        self.assertIn("course_covers_concept", edge_types)
        self.assertIn("knowledge_enables_skill", edge_types)
        self.assertIn("knowledge_informs_attitude", edge_types)

        concepts = {
            node["source"]["key"]: node
            for node in payload["nodes"]
            if node["type"] in {"knowledge", "skill", "attitude"}
        }
        self.assertEqual(concepts["201"]["properties"]["definition"], "")
        self.assertEqual(
            concepts["202"]["properties"]["definition"],
            "사람이 검토한 산정 역량 정의",
        )
        def all_keys(value: object) -> set[str]:
            if isinstance(value, dict):
                return set(value) | {
                    key
                    for child in value.values()
                    for key in all_keys(child)
                }
            if isinstance(value, list):
                return {
                    key
                    for child in value
                    for key in all_keys(child)
                }
            return set()

        self.assertNotIn("source_payload", all_keys(payload))

    def test_default_request_returns_all_ncs_overview(self) -> None:
        payload = build_ncs_knowledge_graph(self.db_path)

        self.assertEqual(payload["mode"], "overview")
        self.assertEqual(payload["focus"]["node_id"], "overview:ncs")
        self.assertNotIn("unit", payload["facets"]["node_types"])
        self.assertEqual(payload["facets"]["node_types"]["overview"], 1)
        self.assertEqual(payload["facets"]["node_types"]["major"], 1)
        self.assertEqual(payload["nodes"][0]["properties"]["unit_count"], 1)

    def test_major_overview_contains_full_classification_path(self) -> None:
        payload = build_ncs_knowledge_graph(self.db_path, major_code="02")

        self.assertEqual(payload["mode"], "major_overview")
        self.assertEqual(payload["focus"]["major_code"], "02")
        node_types = {node["type"] for node in payload["nodes"]}
        self.assertTrue({"major", "middle", "small", "classification"}.issubset(node_types))
        edge_types = {edge["type"] for edge in payload["edges"]}
        self.assertTrue(
            {"contains_middle", "contains_small", "contains_classification"}.issubset(edge_types)
        )

    def test_classification_drilldown_returns_all_unit_candidates(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO competency_units VALUES ('U2', '인사운영', NULL, '4', NULL, 1, 'raw')"
        )
        conn.commit()
        conn.close()

        payload = build_ncs_knowledge_graph(self.db_path, classification_id=1)

        self.assertEqual(payload["mode"], "unit_selection")
        self.assertTrue(payload["selection_required"])
        self.assertEqual({item["unit_code"] for item in payload["candidates"]}, {"U1", "U2"})

    def test_rejected_relations_are_hidden_and_duplicate_evidence_is_preserved(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO ontology_concepts VALUES (204, '폐기 개념', 'knowledge', NULL, 'missing', 'raw')"
        )
        conn.execute(
            "INSERT INTO criteria_concept_links VALUES (304, 101, 204, 'related', 'rejected')"
        )
        conn.execute(
            "INSERT INTO ontology_concept_relations VALUES (403, 202, 203, 'skill_supports_attitude', '폐기 관계', 'rejected')"
        )
        conn.execute(
            "INSERT INTO task_ksa_concept_relations VALUES (404, 101, 202, 203, 'skill_supports_attitude', 0.99, 'rejected')"
        )
        conn.execute(
            "INSERT INTO task_ksa_concept_relations VALUES (405, 101, 201, 202, 'knowledge_enables_skill', 0.88, 'candidate')"
        )
        conn.execute(
            "INSERT INTO ncs_training_course_concept_links VALUES (603, 501, 202, 'training_goal_concept_token', 0.77, '추가 토큰 근거', 'auto_linked')"
        )
        conn.execute(
            "INSERT INTO ncs_training_courses VALUES (502, '폐기 과정', '5', '폐기', '8', '', '')"
        )
        conn.execute(
            "INSERT INTO ncs_training_course_unit_links VALUES (604, 502, 'U1', 'name_only', 0.99, 'rejected')"
        )
        conn.execute("INSERT INTO ncs_qualification_items VALUES ('Q2', '폐기 자격', '시험기관')")
        conn.execute(
            "INSERT INTO ncs_unit_qualification_links VALUES (702, 'U1', 'Q2', 'name_only', 0.99, 'rejected')"
        )
        conn.execute("INSERT INTO ncs_job_base_competencies VALUES (803, '폐기 기초능력')")
        conn.execute(
            "INSERT INTO ncs_unit_job_base_links VALUES (804, 'U1', 803, 'name_only', 0.99, 'rejected')"
        )
        conn.commit()
        conn.close()

        payload = build_ncs_knowledge_graph(self.db_path, unit_code="U1")
        node_source_keys = {node["source"]["key"] for node in payload["nodes"]}
        edge_source_keys = {edge["source_ref"]["key"] for edge in payload["edges"]}

        self.assertNotIn("204", node_source_keys)
        self.assertNotIn("502", node_source_keys)
        self.assertNotIn("Q2", node_source_keys)
        self.assertNotIn("803", node_source_keys)
        self.assertTrue({"403", "404", "604", "702", "804"}.isdisjoint(edge_source_keys))

        relation = next(
            edge
            for edge in payload["edges"]
            if edge["source"] == "concept:201"
            and edge["target"] == "concept:202"
            and edge["type"] == "knowledge_enables_skill"
        )
        self.assertEqual(relation["evidence_count"], 2)
        self.assertEqual(len(relation["evidence_refs"]), 2)

        course_edge = next(
            edge
            for edge in payload["edges"]
            if edge["source"] == "concept:202"
            and edge["target"] == "course:501"
            and edge["type"] == "course_covers_concept"
        )
        self.assertEqual(course_edge["evidence_count"], 2)
        self.assertEqual(len(course_edge["evidence_refs"]), 2)

    def test_ambiguous_query_returns_candidates_without_graph(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO competency_units VALUES ('U2', '인사기획 고도화', NULL, '6', NULL, 1, 'raw')"
        )
        conn.commit()
        conn.close()

        payload = build_ncs_knowledge_graph(self.db_path, query="인사")

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["selection_required"])
        self.assertGreaterEqual(len(payload["candidates"]), 2)
        self.assertEqual(payload["nodes"], [])
        self.assertEqual(payload["edges"], [])

    def test_search_falls_back_to_raw_name_when_refined_name_is_blank(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE competency_units SET unit_name_refined = '   ' WHERE unit_code = 'U1'")
        conn.commit()
        conn.close()

        payload = build_ncs_knowledge_graph(self.db_path, query="인사기획")

        self.assertFalse(payload["selection_required"])
        self.assertEqual(payload["focus"]["unit_code"], "U1")

    def test_node_limit_is_enforced_and_reported(self) -> None:
        conn = sqlite3.connect(self.db_path)
        next_criteria = 1000
        for index in range(20, 32):
            conn.execute(
                "INSERT INTO competency_elements VALUES (?, 'U1', ?, ?, ?, NULL, '5', NULL, 'raw')",
                (index, str(index), f'E{index}', f'추가 요소 {index}'),
            )
            for task_index in range(3):
                conn.execute(
                    "INSERT INTO performance_criteria VALUES (?, ?, ?, ?, NULL, 'raw')",
                    (next_criteria, index, str(task_index + 1), f'추가 수행준거 {next_criteria}'),
                )
                next_criteria += 1
        conn.commit()
        conn.close()

        payload = build_ncs_knowledge_graph(self.db_path, unit_code="U1", max_nodes=24)

        self.assertLessEqual(len(payload["nodes"]), 24)
        self.assertTrue(payload["truncation"]["truncated"])
        self.assertTrue(payload["truncation"]["omitted_by_type"])

    def test_missing_core_table_is_structured_error(self) -> None:
        broken_path = Path(self.tmp.name) / "broken.db"
        conn = sqlite3.connect(broken_path)
        conn.execute("CREATE TABLE classifications (classification_id INTEGER)")
        conn.commit()
        conn.close()

        with self.assertRaises(KnowledgeGraphDataError) as raised:
            build_ncs_knowledge_graph(broken_path)

        payload = raised.exception.to_payload()
        self.assertEqual(payload["error"], "schema_incomplete")
        self.assertIn("competency_units", payload["missing_tables"])

    def test_html_uses_only_local_graph_api_and_vendored_assets(self) -> None:
        page = (ROOT / "scripts" / "ncs_knowledge_graph.html").read_text(encoding="utf-8")
        renderer = ROOT / "scripts" / "vendor" / "3d-force-graph-1.80.0.min.js"
        renderer_license = ROOT / "scripts" / "vendor" / "3d-force-graph-LICENSE.txt"

        self.assertIn("/api/ncs-knowledge-graph", page)
        self.assertIn("NCS 역량 지도", page)
        self.assertIn("/assets/3d-force-graph-1.80.0.min.js", page)
        self.assertIn("전체 NCS", page)
        self.assertIn("major_code", page)
        self.assertIn("classification_id", page)
        self.assertIn('id="graphFallback"', page)
        self.assertIn('id="toggleRenderer"', page)
        self.assertIn("activateFallback", page)
        self.assertIn("verify3DRender", page)
        self.assertIn("blank WebGL canvas", page)
        self.assertIn("fallbackYaw", page)
        self.assertIn("드래그 회전", page)
        self.assertIn("preserveDrawingBuffer:true", page)
        self.assertNotIn("chris.gomdori", page)
        self.assertNotIn('src="http', page)
        self.assertNotIn('href="http', page)
        self.assertNotIn("fetch('http", page)
        self.assertGreater(renderer.stat().st_size, 1_000_000)
        self.assertIn("MIT License", renderer_license.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
