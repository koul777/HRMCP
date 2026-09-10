from __future__ import annotations

import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.gold_lpg import (  # noqa: E402
    FULL_FIDELITY_PROFILE,
    GOLD_LPG_SCHEMA,
    NEO4J_DOMAIN_EDGE_MERGE_CYPHER,
    NEO4J_DOMAIN_NODE_MERGE_CYPHER,
    NEO4J_EDGE_MERGE_CYPHER,
    NEO4J_NODE_MERGE_CYPHER,
    NEO4J_SCHEMA_DDL,
    build_gold_lpg_projection,
    external_id,
    neo4j_import_plan,
    neo4j_import_parameters,
    neo4j_vector_index_ddl,
)
from ncs_mcp.internal_job_roles import (  # noqa: E402
    ContractValidationError,
    InternalJobRole,
    RoleAlignmentCandidate,
)
from ncs_mcp.gold_readiness import GoldProjectionProfile  # noqa: E402


SCHEMA = """
CREATE TABLE classifications (
  classification_id INTEGER PRIMARY KEY, major_code TEXT, major_name TEXT,
  middle_code TEXT, middle_name TEXT, small_code TEXT, small_name TEXT,
  sub_code TEXT, sub_name TEXT, review_status TEXT
);
CREATE TABLE competency_units (
  unit_code TEXT PRIMARY KEY, classification_id INTEGER, unit_name_raw TEXT,
  unit_name_refined TEXT, unit_level_raw TEXT, review_status TEXT
);
CREATE TABLE competency_elements (
  element_id INTEGER PRIMARY KEY, unit_code TEXT, element_no TEXT,
  element_code_raw TEXT, element_name_raw TEXT, element_level_raw TEXT, review_status TEXT
);
CREATE TABLE performance_criteria (
  criteria_id INTEGER PRIMARY KEY, element_id INTEGER, criteria_no TEXT,
  criteria_text_raw TEXT, criteria_text_refined TEXT, review_status TEXT
);
CREATE TABLE ksa_items (ksa_id INTEGER PRIMARY KEY, ksa_text_raw TEXT NOT NULL);
CREATE TABLE ontology_concepts (
  concept_id INTEGER PRIMARY KEY, concept_name TEXT, concept_type TEXT,
  definition TEXT, definition_source TEXT, definition_status TEXT, review_status TEXT
);
CREATE TABLE criteria_concept_links (
  link_id INTEGER PRIMARY KEY, criteria_id INTEGER, concept_id INTEGER,
  relation_type TEXT, link_status TEXT
);
CREATE TABLE task_ksa_concept_relations (
  relation_id INTEGER PRIMARY KEY, criteria_id INTEGER, element_id INTEGER,
  source_concept_id INTEGER, target_concept_id INTEGER, source_atomic_id INTEGER,
  target_atomic_id INTEGER, relation_type TEXT, evidence_text TEXT,
  confidence_score REAL, review_status TEXT
);
CREATE TABLE ncs_training_courses (
  training_course_id INTEGER PRIMARY KEY, ncs_cl_cd TEXT, compe_unit_name TEXT,
  compe_unit_level TEXT, train_goal TEXT, train_time TEXT, fac_name TEXT, meth_name TEXT
);
CREATE TABLE ncs_training_course_unit_links (
  link_id INTEGER PRIMARY KEY, training_course_id INTEGER, unit_code TEXT,
  link_method TEXT, confidence_score REAL, review_status TEXT
);
CREATE TABLE ncs_training_course_concept_links (
  link_id INTEGER PRIMARY KEY, training_course_id INTEGER, unit_code TEXT,
  concept_id INTEGER, link_method TEXT, confidence_score REAL, evidence_text TEXT, review_status TEXT
);
CREATE TABLE ncs_training_course_element_links (
  link_id INTEGER PRIMARY KEY, training_course_id INTEGER, unit_code TEXT,
  element_id INTEGER, link_method TEXT, confidence_score REAL, evidence_text TEXT, review_status TEXT
);
CREATE TABLE training_goal_concept_links (
  link_id INTEGER PRIMARY KEY, training_course_id INTEGER, unit_code TEXT,
  element_id INTEGER, concept_id INTEGER, link_method TEXT, confidence_score REAL,
  evidence_text TEXT, review_status TEXT
);
CREATE TABLE training_delivery_relations (
  relation_id INTEGER PRIMARY KEY, training_course_id INTEGER, relation_type TEXT,
  relation_value TEXT, normalized_value TEXT, numeric_value REAL, evidence_text TEXT,
  confidence_score REAL, review_status TEXT
);
"""


class GoldLpgProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "fixture.db"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT INTO classifications VALUES (1, '02', 'Management', '01', 'HR', '01', 'Plan', '01', 'Planning', 'raw')")
            conn.execute("INSERT INTO competency_units VALUES ('U 1', 1, 'HR Planning', NULL, '5', 'raw')")
            conn.execute("INSERT INTO competency_elements VALUES (10, 'U 1', '1', 'E1', 'Plan work', '5', 'raw')")
            conn.execute("INSERT INTO performance_criteria VALUES (100, 10, '1.1', 'Analyse workforce needs', NULL, 'raw')")
            conn.execute("INSERT INTO performance_criteria VALUES (101, 10, '1.2', 'Confirm workforce needs', NULL, 'raw')")
            conn.execute("INSERT INTO ksa_items VALUES (1000, 'immutable raw KSA')")
            conn.executemany(
                "INSERT INTO ontology_concepts VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (200, 'Planning knowledge', 'knowledge', 'Planning knowledge: generic boilerplate', 'boilerplate', 'defined', 'raw'),
                    (201, 'Interview skill', 'skill', 'A human-approved skill definition.', 'human_packet', 'defined', 'human_reviewed'),
                    (202, 'Careful attitude', 'attitude', None, None, 'missing', 'candidate'),
                ],
            )
            conn.execute("INSERT INTO criteria_concept_links VALUES (300, 100, 200, 'requires', 'auto_linked')")
            conn.execute("INSERT INTO criteria_concept_links VALUES (301, 101, 200, 'requires', 'candidate')")
            conn.execute("INSERT INTO task_ksa_concept_relations VALUES (301, 100, 10, 200, 202, 1, 2, 'knowledge_informs_attitude', 'same task', .72, 'candidate')")
            conn.execute("INSERT INTO ncs_training_courses VALUES (400, 'U 1', 'HR planning course', '5', 'Learn planning', '24', 'classroom', 'practice')")
            conn.execute("INSERT INTO ncs_training_course_unit_links VALUES (401, 400, 'U 1', 'exact', 1.0, 'auto_linked')")
            conn.execute("INSERT INTO ncs_training_course_concept_links VALUES (402, 400, 'U 1', 201, 'goal_text', .9, 'goal evidence', 'auto_linked')")
            conn.execute("INSERT INTO ncs_training_course_concept_links VALUES (403, 400, 'U 1', 200, 'unit_ksa_concept_inherited', .4, 'inherited evidence', 'auto_linked')")
            conn.execute("INSERT INTO ncs_training_course_element_links VALUES (403, 400, 'U 1', 10, 'element', .8, 'element evidence', 'auto_linked')")
            conn.execute("INSERT INTO training_goal_concept_links VALUES (404, 400, 'U 1', 10, 201, 'goal_text', .95, 'direct goal', 'auto_linked')")
            conn.execute("INSERT INTO training_delivery_relations VALUES (405, 400, 'method', 'practice', 'practice', NULL, 'delivery evidence', 1.0, 'auto_linked')")
            conn.commit()

    def test_serving_core_is_deterministic_and_collapses_criterion_task(self) -> None:
        first = build_gold_lpg_projection(self.db_path)
        second = build_gold_lpg_projection(self.db_path)

        self.assertEqual(first, second)
        self.assertEqual(first["schema"], GOLD_LPG_SCHEMA)
        self.assertEqual(first["manifest"]["projection_fingerprint"], second["manifest"]["projection_fingerprint"])
        self.assertEqual(first["profile"]["name"], "serving_core")
        labels = {label for node in first["nodes"] for label in node["labels"]}
        self.assertIn("PerformanceCriterion", labels)
        self.assertIn("Task", labels)
        criterion_nodes = [node for node in first["nodes"] if "PerformanceCriterion" in node["labels"]]
        self.assertTrue(all("Task" in node["labels"] for node in criterion_nodes))
        self.assertFalse(any(node["properties"]["node_type"] == "task" for node in first["nodes"]))
        self.assertFalse(any(edge["type"] == "REPRESENTS_TASK" for edge in first["edges"]))
        self.assertFalse(any(edge["type"] == "TASK_KSA_CONCEPT_RELATION" for edge in first["edges"]))
        self.assertFalse(any(edge["type"] == "HAS_ELEMENT" for edge in first["edges"]))
        self.assertIn("task_ksa_concept_relations_omitted", {item["code"] for item in first["diagnostics"]})

    def test_full_fidelity_is_explicit_and_serving_summarizes_element_ksa(self) -> None:
        serving = build_gold_lpg_projection(self.db_path)
        full = build_gold_lpg_projection(self.db_path, profile=FULL_FIDELITY_PROFILE)
        serving_labels = {node["id"]: set(node["labels"]) for node in serving["nodes"]}
        full_labels = {node["id"]: set(node["labels"]) for node in full["nodes"]}

        self.assertEqual(full["profile"]["name"], "full_fidelity")
        self.assertTrue(any(node["properties"]["node_type"] == "task" for node in full["nodes"]))
        self.assertTrue(any(edge["type"] == "REPRESENTS_TASK" for edge in full["edges"]))
        self.assertTrue(any(edge["type"] == "TASK_KSA_CONCEPT_RELATION" for edge in full["edges"]))
        self.assertTrue(any(edge["type"] == "HAS_ELEMENT" for edge in full["edges"]))
        serving_element_ksa = [
            edge for edge in serving["edges"]
            if edge["type"] == "REQUIRES_KNOWLEDGE"
            and edge["properties"].get("derived_from") == "criteria_concept_links"
            and "PerformanceElement" in serving_labels[edge["source"]]
        ]
        full_element_ksa = [
            edge for edge in full["edges"]
            if edge["type"] == "REQUIRES_KNOWLEDGE"
            and edge["properties"].get("derived_from") == "criteria_concept_links"
            and "PerformanceElement" in full_labels[edge["source"]]
        ]
        self.assertEqual(len(serving_element_ksa), 1)
        self.assertEqual(serving_element_ksa[0]["properties"]["source_link_count"], 2)
        self.assertEqual(serving_element_ksa[0]["properties"]["distinct_criteria_count"], 2)
        self.assertEqual(len(full_element_ksa), 2)
        self.assertFalse(any(
            edge["type"] == "COURSE_COVERS_CONCEPT"
            and edge["properties"].get("link_method") == "unit_ksa_concept_inherited"
            for edge in serving["edges"]
        ))
        self.assertTrue(any(
            edge["type"] == "COURSE_COVERS_CONCEPT"
            and edge["properties"].get("link_method") == "unit_ksa_concept_inherited"
            for edge in full["edges"]
        ))

    def test_serving_core_aggregates_one_direct_job_to_ksa_edge(self) -> None:
        first = build_gold_lpg_projection(self.db_path)
        second = build_gold_lpg_projection(self.db_path)
        full = build_gold_lpg_projection(self.db_path, profile=FULL_FIDELITY_PROFILE)
        labels = {node["id"]: set(node["labels"]) for node in first["nodes"]}
        job_concept_edges = [
            edge for edge in first["edges"]
            if edge["type"] == "REQUIRES_KNOWLEDGE"
            and "NCSJob" in labels[edge["source"]]
            and "KSAConcept" in labels[edge["target"]]
        ]
        full_labels = {node["id"]: set(node["labels"]) for node in full["nodes"]}

        self.assertEqual(first["manifest"]["summarized"]["job_concept_edges"], 1)
        self.assertEqual(len(job_concept_edges), 1)
        edge = job_concept_edges[0]
        self.assertEqual(edge["properties"]["summary_scope"], "ncs_job_to_ksa")
        self.assertEqual(edge["properties"]["source_link_count"], 2)
        self.assertEqual(edge["properties"]["distinct_unit_count"], 1)
        self.assertEqual(edge["properties"]["distinct_element_count"], 1)
        self.assertEqual(edge["properties"]["distinct_criteria_count"], 2)
        self.assertLessEqual(len(edge["properties"]["source_link_key_samples"]), 5)
        self.assertEqual(first["manifest"]["projection_fingerprint"], second["manifest"]["projection_fingerprint"])
        self.assertIn("job_concept_links_summarized", {item["code"] for item in first["diagnostics"]})
        self.assertFalse(any(
            edge["type"].startswith("REQUIRES_")
            and "NCSJob" in full_labels.get(edge["source"], set())
            and edge["properties"].get("summary_scope") == "ncs_job_to_ksa"
            for edge in full["edges"]
        ))
        self.assertFalse(any(edge["type"] == "HAS_NCS_JOB" for edge in first["edges"]))
        self.assertTrue(any(edge["type"] == "HAS_NCS_JOB" for edge in full["edges"]))

    def test_provenance_and_trusted_definition_policy(self) -> None:
        projection = build_gold_lpg_projection(self.db_path)
        concept_nodes = {node["properties"]["concept_id"]: node for node in projection["nodes"] if node["properties"]["node_type"] == "ontology_concept"}

        self.assertEqual(concept_nodes["200"]["properties"]["definition_is_trusted"], False)
        self.assertNotIn("definition", concept_nodes["200"]["properties"])
        self.assertEqual(concept_nodes["201"]["properties"]["definition"], "A human-approved skill definition.")
        self.assertIn("Skill", concept_nodes["201"]["labels"])
        edge = next(edge for edge in projection["edges"] if edge["type"] == "COURSE_GOAL_COVERS_CONCEPT")
        self.assertEqual(edge["provenance"]["source_table"], "training_goal_concept_links")
        self.assertEqual(edge["provenance"]["review_status"], "auto_linked")
        self.assertNotIn("human_reviewed", str(projection["edges"]))

    def test_source_is_read_only_and_raw_ksa_is_unchanged(self) -> None:
        before_hash = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        with closing(sqlite3.connect(self.db_path)) as conn:
            before_raw = conn.execute("SELECT ksa_text_raw FROM ksa_items WHERE ksa_id = 1000").fetchone()[0]

        projection = build_gold_lpg_projection(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as conn:
            after_raw = conn.execute("SELECT ksa_text_raw FROM ksa_items WHERE ksa_id = 1000").fetchone()[0]
        self.assertEqual(before_hash, hashlib.sha256(self.db_path.read_bytes()).hexdigest())
        self.assertEqual(before_raw, after_raw)
        self.assertTrue(projection["read_only"])
        self.assertFalse(projection["db_writes"])

    def test_cypher_uses_parameterized_merge_identifiers(self) -> None:
        projection = build_gold_lpg_projection(self.db_path)
        parameters = neo4j_import_parameters(projection)

        self.assertIn("UNWIND $nodes AS row", NEO4J_NODE_MERGE_CYPHER)
        self.assertIn("MERGE (node:LpgNode {id: row.id})", NEO4J_NODE_MERGE_CYPHER)
        self.assertIn("SET node:NCSJobCategory", NEO4J_NODE_MERGE_CYPHER)
        self.assertIn("UNWIND $edges AS row", NEO4J_EDGE_MERGE_CYPHER)
        self.assertIn("MERGE (source)-[edge:HAS_SUB_CATEGORY {id: row.id}]->(target)", NEO4J_EDGE_MERGE_CYPHER)
        self.assertTrue(all(" IF NOT EXISTS " in statement for statement in NEO4J_SCHEMA_DDL))
        self.assertEqual(parameters["nodes"], projection["nodes"])
        self.assertEqual(parameters["edges"], projection["edges"])

    def test_domain_templates_and_exact_category_to_ksa_path(self) -> None:
        projection = build_gold_lpg_projection(self.db_path)
        node_cypher = "\n".join(NEO4J_DOMAIN_NODE_MERGE_CYPHER.values())
        edge_cypher = "\n".join(NEO4J_DOMAIN_EDGE_MERGE_CYPHER.values())

        for label in (
            "NCSJobCategory", "NCSJob", "CompetencyUnit", "PerformanceElement",
            "PerformanceCriterion", "Task", "KSAConcept", "Knowledge", "Skill",
            "Attitude", "TrainingCourse", "TrainingDelivery", "InternalJobRole",
        ):
            self.assertIn(f":{label}", node_cypher)
        for relation_type in (
            "HAS_SUB_CATEGORY", "REQUIRES_UNIT", "DEFINED_BY", "HAS_CRITERION",
            "REQUIRES_KNOWLEDGE", "REQUIRES_SKILL", "REQUIRES_ATTITUDE",
            "REPRESENTS_TASK", "ALIGNED_TO",
        ):
            self.assertIn(f"[edge:{relation_type}", edge_cypher)
        self.assertNotIn("apoc", (node_cypher + edge_cypher).lower())
        self.assertIn("SET node = row.properties", node_cypher)
        self.assertIn("SET edge = row.properties", edge_cypher)

        labels_by_id = {node["id"]: set(node["labels"]) for node in projection["nodes"]}
        edge_by_type = {}
        for edge in projection["edges"]:
            edge_by_type.setdefault(edge["type"], []).append(edge)
        job_edge = next(
            edge for edge in edge_by_type["HAS_SUB_CATEGORY"]
            if "NCSJob" in labels_by_id[edge["target"]]
        )
        category_edge = next(
            edge for edge in edge_by_type["HAS_SUB_CATEGORY"]
            if edge["target"] == job_edge["source"]
        )
        unit_edge = edge_by_type["REQUIRES_UNIT"][0]
        element_edge = next(
            edge for edge in edge_by_type["DEFINED_BY"]
            if "CompetencyUnit" in labels_by_id[edge["source"]]
            and "PerformanceElement" in labels_by_id[edge["target"]]
        )
        criterion_edge = edge_by_type["HAS_CRITERION"][0]
        ksa_edge = next(
            edge for edge in edge_by_type["REQUIRES_KNOWLEDGE"]
            if "PerformanceCriterion" in labels_by_id[edge["source"]]
        )
        self.assertIn("NCSJobCategory", labels_by_id[category_edge["source"]])
        self.assertIn("NCSJob", labels_by_id[job_edge["target"]])
        self.assertIn("CompetencyUnit", labels_by_id[unit_edge["target"]])
        self.assertIn("PerformanceElement", labels_by_id[element_edge["target"]])
        self.assertIn("PerformanceCriterion", labels_by_id[criterion_edge["target"]])
        self.assertIn("KSAConcept", labels_by_id[ksa_edge["target"]])
        self.assertEqual(unit_edge["source"], job_edge["target"])
        self.assertEqual(element_edge["source"], unit_edge["target"])
        self.assertEqual(criterion_edge["source"], element_edge["target"])
        self.assertEqual(ksa_edge["source"], criterion_edge["target"])
        self.assertEqual(ksa_edge["properties"]["criteria_id"], "100")
        self.assertEqual(ksa_edge["provenance"]["source_table"], "criteria_concept_links")
        self.assertTrue(any(
            edge["source"] == element_edge["target"]
            and edge["type"] == "REQUIRES_KNOWLEDGE"
            and edge["properties"].get("derived_from") == "criteria_concept_links"
            for edge in projection["edges"]
        ))

        plan = neo4j_import_plan(projection)
        self.assertFalse(plan["internal_job_role_source_available"])
        self.assertEqual(plan["internal_job_role_template"], NEO4J_DOMAIN_NODE_MERGE_CYPHER["InternalJobRole"])
        self.assertNotIn("InternalJobRole", {item["group"] for item in plan["node_operations"]})
        self.assertFalse(any("InternalJobRole" in node["labels"] for node in projection["nodes"]))

    def test_trusted_definition_downgrade_replaces_owned_properties(self) -> None:
        trusted = build_gold_lpg_projection(self.db_path)
        trusted_node = next(
            node for node in trusted["nodes"]
            if node["properties"].get("concept_id") == "201"
        )
        self.assertIn("definition", trusted_node["properties"])

        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE ontology_concepts SET definition_status = 'missing', review_status = 'candidate' WHERE concept_id = 201"
            )
            conn.commit()
        downgraded = build_gold_lpg_projection(self.db_path)
        downgraded_node = next(
            node for node in downgraded["nodes"]
            if node["properties"].get("concept_id") == "201"
        )
        self.assertNotIn("definition", downgraded_node["properties"])
        self.assertFalse(downgraded_node["properties"]["definition_is_trusted"])
        self.assertIn("SET node = row.properties", NEO4J_DOMAIN_NODE_MERGE_CYPHER["KSAConceptSkill"])
        skill_template = NEO4J_DOMAIN_NODE_MERGE_CYPHER["KSAConceptSkill"]
        self.assertIn("MERGE (node:LpgNode {id: row.id})", skill_template)
        self.assertIn("REMOVE node:Knowledge:Skill:Attitude", skill_template)
        self.assertIn("SET node:KSAConcept:Skill", skill_template)

    def test_ncs_job_uses_stable_code_path_not_classification_surrogate(self) -> None:
        projection = build_gold_lpg_projection(self.db_path)
        job = next(node for node in projection["nodes"] if "NCSJob" in node["labels"])

        self.assertEqual(job["properties"]["code"], "02010101")
        self.assertEqual(job["id"], external_id("ncs_job", "02010101"))
        self.assertNotIn("classification_id", job["properties"])
        self.assertEqual(job["provenance"]["classification_id"], "1")

    def test_incomplete_classification_path_does_not_create_a_job_or_unit_path(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO classifications VALUES (2, '03', 'Sales', NULL, NULL, '01', 'Skipped', '01', 'Invalid', 'raw')"
            )
            conn.execute("INSERT INTO competency_units VALUES ('U 2', 2, 'Invalid unit', NULL, '4', 'raw')")
            conn.commit()

        projection = build_gold_lpg_projection(self.db_path)
        jobs = [node for node in projection["nodes"] if "NCSJob" in node["labels"]]
        category_paths = {
            node["properties"].get("path") for node in projection["nodes"]
            if "NCSJobCategory" in node["labels"]
        }

        self.assertEqual([job["properties"]["code"] for job in jobs], ["02010101"])
        self.assertIn("03", category_paths)
        self.assertNotIn("03:01", category_paths)
        self.assertFalse(any(
            edge["type"] == "REQUIRES_UNIT"
            and edge["target"] == external_id("competency_unit", "U 2")
            for edge in projection["edges"]
        ))
        self.assertTrue(any(
            item["code"] == "ncs_job_code_unresolved" and item["classification_id"] == "2"
            for item in projection["diagnostics"]
        ))

    def test_blank_classification_code_does_not_create_a_job_or_unit_path(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO classifications VALUES (3, '04', 'Finance', '', '', '01', 'Skipped', '01', 'Invalid', 'raw')"
            )
            conn.execute("INSERT INTO competency_units VALUES ('U 3', 3, 'Invalid unit', NULL, '4', 'raw')")
            conn.commit()

        projection = build_gold_lpg_projection(self.db_path)

        self.assertFalse(any(
            "NCSJob" in node["labels"] and node["properties"].get("code", "").startswith("04")
            for node in projection["nodes"]
        ))
        self.assertFalse(any(
            edge["type"] == "REQUIRES_UNIT"
            and edge["target"] == external_id("competency_unit", "U 3")
            for edge in projection["edges"]
        ))
        self.assertTrue(any(
            item["code"] == "ncs_job_code_unresolved" and item["classification_id"] == "3"
            for item in projection["diagnostics"]
        ))

    def test_rejects_noncanonical_projection_profile_flags(self) -> None:
        with self.assertRaises(ValueError):
            build_gold_lpg_projection(
                self.db_path,
                profile=GoldProjectionProfile(
                    name="serving_core", include_training_courses=False,
                ),
            )
        with self.assertRaises(ValueError):
            build_gold_lpg_projection(
                self.db_path,
                profile=GoldProjectionProfile(
                    name="full_fidelity", include_task_ksa_detailed_relations=False,
                ),
            )

    def test_internal_role_alignment_projects_tenant_safe_candidate_path(self) -> None:
        role = InternalJobRole(
            organization_namespace="Acme HR",
            role_id="people-partner",
            display_name="People Partner",
            duties=("Workforce planning",),
            provenance={"source": "tenant_catalog"},
        )
        alignment = RoleAlignmentCandidate(
            role_gold_id=role.gold_id,
            ncs_target_type="ncs_job",
            ncs_target_key="02010101",
            score=0.82,
            method="semantic_embedding",
            model="local-model-v1",
            evidence=({"source": "role_description", "text": "workforce planning"},),
            status="candidate",
            provenance={"source": "alignment_run"},
        )
        projection = build_gold_lpg_projection(
            self.db_path, internal_roles=[role], role_alignments=[alignment]
        )
        role_node = next(node for node in projection["nodes"] if "InternalJobRole" in node["labels"])
        alignment_edge = next(edge for edge in projection["edges"] if edge["type"] == "ALIGNED_TO")
        unit_edge = next(edge for edge in projection["edges"] if edge["type"] == "REQUIRES_UNIT")
        element_edge = next(
            edge for edge in projection["edges"]
            if edge["type"] == "DEFINED_BY" and edge["source"] == unit_edge["target"]
        )

        self.assertEqual(role_node["properties"]["role_gold_id"], role.gold_id)
        self.assertEqual(alignment_edge["source"], role_node["id"])
        self.assertEqual(alignment_edge["target"], unit_edge["source"])
        self.assertEqual(element_edge["source"], unit_edge["target"])
        self.assertEqual(alignment_edge["properties"]["score"], 0.82)
        self.assertEqual(alignment_edge["properties"]["status"], "candidate")
        self.assertIsInstance(alignment_edge["properties"]["evidence_json"], str)
        self.assertTrue(neo4j_import_plan(projection)["internal_job_role_source_available"])

    def test_cross_tenant_role_candidate_is_rejected(self) -> None:
        tenant_a = InternalJobRole("Tenant A", "role-1", "Planner")
        tenant_b = InternalJobRole("Tenant B", "role-1", "Planner")
        candidate = RoleAlignmentCandidate(
            role_gold_id=tenant_b.gold_id,
            ncs_target_type="ncs_job",
            ncs_target_key="02010101",
            score=0.7,
            method="test",
        )

        with self.assertRaises(ContractValidationError):
            build_gold_lpg_projection(
                self.db_path, internal_roles=[tenant_a], role_alignments=[candidate]
            )

    def test_unresolved_alignment_stays_candidate_metadata_without_edge(self) -> None:
        role = InternalJobRole("Acme", "role-1", "Planner")
        unresolved = RoleAlignmentCandidate(
            role_gold_id=role.gold_id,
            ncs_target_type="ncs_job",
            ncs_target_key="99999999",
            score=0.6,
            method="test",
            status="unresolved",
        )
        projection = build_gold_lpg_projection(
            self.db_path, internal_roles=[role], role_alignments=[unresolved]
        )
        role_node = next(node for node in projection["nodes"] if "InternalJobRole" in node["labels"])

        self.assertFalse(any(edge["type"] == "ALIGNED_TO" for edge in projection["edges"]))
        self.assertEqual(role_node["properties"]["alignment_candidate_count"], 1)
        self.assertIn("unresolved", role_node["properties"]["alignment_candidates_json"])
        self.assertIn("role_alignment_not_linked_status", {item["code"] for item in projection["diagnostics"]})

    def test_vector_index_ddl_is_opt_in_and_validated(self) -> None:
        statements = neo4j_vector_index_ddl(768)

        self.assertEqual(
            statements,
            (
                "CREATE VECTOR INDEX ncs_lpg_performance_criterion_embedding IF NOT EXISTS "
                "FOR (node:PerformanceCriterion) ON (node.embedding) "
                "OPTIONS {indexConfig: {`vector.dimensions`: 768, "
                "`vector.similarity_function`: 'cosine'}}",
                "CREATE VECTOR INDEX ncs_lpg_performance_element_embedding IF NOT EXISTS "
                "FOR (node:PerformanceElement) ON (node.embedding) "
                "OPTIONS {indexConfig: {`vector.dimensions`: 768, "
                "`vector.similarity_function`: 'cosine'}}",
                "CREATE VECTOR INDEX ncs_lpg_ksa_concept_embedding IF NOT EXISTS "
                "FOR (node:KSAConcept) ON (node.embedding) "
                "OPTIONS {indexConfig: {`vector.dimensions`: 768, "
                "`vector.similarity_function`: 'cosine'}}",
            ),
        )
        self.assertTrue(all(statement.endswith("'cosine'}}") for statement in statements))
        self.assertEqual(len(neo4j_vector_index_ddl(4096)), 3)
        for invalid in (True, False, 0, -1, 4097, 12.0):
            with self.assertRaises(ValueError):
                neo4j_vector_index_ddl(invalid)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
