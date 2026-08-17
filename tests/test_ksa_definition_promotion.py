from __future__ import annotations

import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ncs_mcp.db import (
    audit_ksa_definition_review_decision_csv,
    build_ksa_definition_review_decision_action_plan,
    build_duplicate_concept_relations,
    build_ksa_definition_priority_review_pack,
    connect,
    initialize_database,
    ksa_definition_promotion_status,
    now_utc,
    promote_ksa_definitions,
    promote_top_concepts_by_frequency,
    rank_concepts_by_recommendation_frequency,
    retract_boilerplate_definitions,
    _is_ksa_definition_boilerplate,
    _korean_object_particle,
    _term_definition_text_for_concept,
    write_ksa_definition_priority_review_pack_markdown,
)


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "ncs_harness.py"


class KsaDefinitionPromotionTests(unittest.TestCase):
    def _memory_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialize_database(conn)
        return conn

    def _insert_concept(
        self,
        conn,
        concept_name: str,
        *,
        concept_type: str = "knowledge",
        normalized_key: str | None = None,
        review_status: str = "raw",
        definition_status: str = "missing",
    ) -> int:
        timestamp = now_utc()
        conn.execute(
            """
            INSERT INTO ontology_concepts(
                concept_name, normalized_key, concept_type,
                definition, definition_source, definition_status,
                relation_status, review_status, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, NULL, ?, 'unlinked', ?, ?, ?)
            """,
            (
                concept_name,
                normalized_key or concept_name.replace(" ", "").lower(),
                concept_type,
                definition_status,
                review_status,
                timestamp,
                timestamp,
            ),
        )
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _seed_fixture(self, conn) -> tuple[int, int, str, str]:
        timestamp = now_utc()

        conn.execute(
            """
            INSERT INTO ontology_concepts(
                concept_name, normalized_key, concept_type,
                definition, definition_source, definition_status,
                relation_status, review_status, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, NULL, 'missing', 'unlinked', 'raw', ?, ?)
            """,
            (
                "Workforce planning knowledge",
                "workforceplanningknowledge",
                "knowledge",
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO ontology_concepts(
                concept_name, normalized_key, concept_type,
                definition, definition_source, definition_status,
                relation_status, review_status, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, NULL, 'missing', 'unlinked', 'raw', ?, ?)
            """,
            (
                "Workforce planning skill",
                "workforceplanningskill",
                "skill",
                timestamp,
                timestamp,
            ),
        )

        boilerplate_concept_id = conn.execute(
            "SELECT concept_id FROM ontology_concepts WHERE normalized_key = 'workforceplanningknowledge'"
        ).fetchone()["concept_id"]
        real_concept_id = conn.execute(
            "SELECT concept_id FROM ontology_concepts WHERE normalized_key = 'workforceplanningskill'"
        ).fetchone()["concept_id"]

        boilerplate_text = (
            "Workforce planning knowledge: "
            "업무 판단과 문제 해결에 필요한 관련 원리, 기준, 절차, 사례에 대한 지식."
        )
        real_text = "Workforce planning skill: 인력 수요를 분석하고 실행 계획을 수립하는 기술."

        conn.execute(
            """
            INSERT INTO ksa_meaning_candidates(
                concept_id, concept_type, meaning_role, meaning_text,
                source_method, evidence_text, unit_code, element_id,
                criteria_id, ksa_id, confidence_score, review_status,
                created_at, updated_at
            ) VALUES (?, 'knowledge', 'term_definition_candidate',
                      ?, 'term_definition_template',
                      'fixture evidence', NULL, NULL, NULL, NULL, 0.91,
                      'llm_reviewed', ?, ?)
            """,
            (boilerplate_concept_id, boilerplate_text, timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO ksa_meaning_candidates(
                concept_id, concept_type, meaning_role, meaning_text,
                source_method, evidence_text, unit_code, element_id,
                criteria_id, ksa_id, confidence_score, review_status,
                created_at, updated_at
            ) VALUES (?, 'skill', 'term_definition_candidate',
                      ?, 'term_definition_template',
                      'fixture evidence', NULL, NULL, NULL, NULL, 0.93,
                      'llm_reviewed', ?, ?)
            """,
            (real_concept_id, real_text, timestamp, timestamp),
        )
        conn.commit()
        return boilerplate_concept_id, real_concept_id, boilerplate_text, real_text

    def _seed_minimal_task_relation(self, conn, source_concept_id: int, target_concept_id: int) -> None:
        timestamp = now_utc()
        seed_counter = int(getattr(self, "_relation_seed_counter", 0)) + 1
        self._relation_seed_counter = seed_counter
        conn.execute(
            """
            INSERT OR IGNORE INTO classifications(
                major_code, major_name, middle_code, middle_name,
                small_code, small_name, sub_code, sub_name
            ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
            """
        )
        classification_id = conn.execute("SELECT classification_id FROM classifications LIMIT 1").fetchone()[
            "classification_id"
        ]
        conn.execute(
            """
            INSERT OR IGNORE INTO competency_units(
                unit_code, base_unit_code, unit_version, unit_name_raw,
                unit_level_raw, classification_id, created_at, updated_at
            ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                      '5', ?, ?, ?)
            """,
            (classification_id, timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO competency_elements(
                unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
            ) VALUES ('0202020101_23v3', ?, ?, ?, '5')
            """,
            (
                f"{source_concept_id}-{seed_counter}",
                f"0202020101_23v3 {source_concept_id}-{seed_counter}",
                f"Element {source_concept_id}-{seed_counter}",
            ),
        )
        element_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw) VALUES (?, '1', 'Criterion')",
            (element_id,),
        )
        criteria_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """
            INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
            VALUES (?, '01', 'knowledge', '1', 'fixture ksa')
            """,
            (element_id,),
        )
        ksa_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """
            INSERT INTO ksa_atomic_items(
                ksa_id, element_id, ksa_type_name, atom_index, atom_text,
                normalized_key, split_method, review_status, created_at
            ) VALUES (?, ?, 'knowledge', 0, 'fixture ksa', ?, 'test', 'raw', ?)
            """,
            (ksa_id, element_id, f"fixtureksa{source_concept_id}{seed_counter}", timestamp),
        )
        atomic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for concept_id in {source_concept_id, target_concept_id}:
            conn.execute(
                """
                INSERT OR IGNORE INTO ksa_atomic_concept_links(atomic_id, concept_id, link_status, created_at)
                VALUES (?, ?, 'raw', ?)
                """,
                (atomic_id, concept_id, timestamp),
            )
        conn.execute(
            """
            INSERT INTO task_ksa_concept_relations(
                criteria_id, element_id, source_concept_id, relation_type,
                target_concept_id, source_atomic_id, target_atomic_id,
                evidence_text, confidence_score, review_status, created_at
            ) VALUES (?, ?, ?, 'co_required_in_element', ?, ?, ?,
                      'fixture relation', 0.8, 'candidate', ?)
            """,
            (
                criteria_id,
                element_id,
                source_concept_id,
                target_concept_id,
                atomic_id,
                atomic_id,
                timestamp,
            ),
        )
        conn.commit()

    def test_term_definition_text_is_word_style_candidate_but_generated_template_is_blocked(self) -> None:
        skill_name = "\ub370\uc774\ud130 \ubd84\uc11d \ub2a5\ub825"
        skill_core_name = "\ub370\uc774\ud130 \ubd84\uc11d"
        vowel_skill_name = "\uc678\uad6d\uc5b4 \ub3c5\ud574\ub2a5\ub825"
        clause_skill_name = "\ubcf4\uace0\uc11c\ub97c \uc791\uc131\ud560 \uc218 \uc788\ub294 \ub2a5\ub825"
        parenthetical_skill_name = "\uace0\uac1d\uad00\uacc4\uad00\ub9ac(CRM) \uae30\uc220"
        knowledge_name = "\uac1c\uc778\uc815\ubcf4 \ubcf4\ud638\ubc95"
        related_knowledge_name = "\ubb38\ud5cc\uc870\uc0ac \ubc29\ubc95\uc5d0 \ub300\ud55c \uc9c0\uc2dd"
        method_knowledge_name = "\uc778\uc0ac\uc804\ub7b5 \ud658\uacbd\ubd84\uc11d\ubc95"
        attitude_name = "\ud488\uc9c8 \uc900\uc218 \ud0dc\ub3c4"
        clause_attitude_name = "\uac1d\uad00\uc801\uc73c\ub85c \uc0ac\uace0\ud558\ub824\ub294 \uc758\uc9c0"

        self.assertEqual(_korean_object_particle("\ub3c5\ud574"), "\ub97c")
        self.assertEqual(_korean_object_particle("\ubd84\uc11d"), "\uc744")
        self.assertEqual(_korean_object_particle("\uace0\uac1d\uad00\uacc4\uad00\ub9ac(CRM)"), "\ub97c")
        skill_text = _term_definition_text_for_concept(
            {"concept_name": skill_name, "concept_type": "skill"}
        )
        vowel_skill_text = _term_definition_text_for_concept(
            {"concept_name": vowel_skill_name, "concept_type": "skill"}
        )
        clause_skill_text = _term_definition_text_for_concept(
            {"concept_name": clause_skill_name, "concept_type": "skill"}
        )
        parenthetical_skill_text = _term_definition_text_for_concept(
            {"concept_name": parenthetical_skill_name, "concept_type": "skill"}
        )
        knowledge_text = _term_definition_text_for_concept(
            {"concept_name": knowledge_name, "concept_type": "knowledge"}
        )
        related_knowledge_text = _term_definition_text_for_concept(
            {"concept_name": related_knowledge_name, "concept_type": "knowledge"}
        )
        method_knowledge_text = _term_definition_text_for_concept(
            {"concept_name": method_knowledge_name, "concept_type": "knowledge"}
        )
        attitude_text = _term_definition_text_for_concept(
            {"concept_name": attitude_name, "concept_type": "attitude"}
        )
        clause_attitude_text = _term_definition_text_for_concept(
            {"concept_name": clause_attitude_name, "concept_type": "attitude"}
        )

        self.assertIn(f"{skill_name}: {skill_core_name}", skill_text)
        self.assertIn("\uc790\ub8cc\ub97c \uc218\uc9d1, \uc815\ub9ac, \ud574\uc11d", skill_text)
        self.assertNotIn("\uad00\ub828 \uc808\ucc28\ub098 \ub3c4\uad6c\ub97c \ud65c\uc6a9\ud574", skill_text)
        self.assertIn("\uc678\uad6d\uc5b4 \ub3c5\ud574\ub97c", vowel_skill_text)
        self.assertNotIn("\ub3c5\ud574\uc744", vowel_skill_text)
        self.assertIn("\ubcf4\uace0\uc11c \uc791\uc131\uc744", clause_skill_text)
        self.assertNotIn("\uc791\uc131\ud560 \uc218 \uc788\ub294\uc744", clause_skill_text)
        self.assertIn("\uace0\uac1d\uad00\uacc4\uad00\ub9ac(CRM)\ub97c", parenthetical_skill_text)
        self.assertNotIn("(CRM)\uc744", parenthetical_skill_text)
        self.assertIn("\uc801\uc6a9 \uc694\uac74\uacfc \uc900\uc218 \uae30\uc900", knowledge_text)
        self.assertNotIn("\uad00\ub828 \uc6d0\ub9ac, \uae30\uc900, \uc808\ucc28, \uc0ac\ub840", knowledge_text)
        self.assertIn("\ubb38\ud5cc\uc870\uc0ac \ubc29\ubc95\uc758 \uc758\ubbf8", related_knowledge_text)
        self.assertNotIn("\ub300\ud55c\uc758", related_knowledge_text)
        self.assertIn("\uc778\uc0ac\uc804\ub7b5 \ud658\uacbd\ubd84\uc11d\ubc95\uc5d0 \ud544\uc694\ud55c \uc790\ub8cc\uc640 \ud310\ub2e8 \uae30\uc900", method_knowledge_text)
        self.assertNotIn("\uc801\uc6a9 \uc694\uac74\uacfc \uc900\uc218 \uae30\uc900", method_knowledge_text)
        self.assertIn("\ud488\uc9c8 \uc900\uc218\ub97c \uae30\uc900", attitude_text)
        self.assertNotIn("\uc900\uc218\uc744 \uae30\uc900", attitude_text)
        self.assertIn("\uac1d\uad00\uc801 \uc0ac\uace0\ub97c \uae30\uc900", clause_attitude_text)
        self.assertNotIn("\uc0ac\uace0\ud558\ub824\ub294\uc744", clause_attitude_text)
        self.assertIn("\uacb0\uacfc\uc758 \uc815\ud655\uc131\uacfc \uae30\uc900 \uc900\uc218", attitude_text)
        self.assertNotIn("\ud488\uc9c8, \ud611\uc5c5, \ucc45\uc784\uc131\uc744 \uc720\uc9c0", attitude_text)
        self.assertTrue(_is_ksa_definition_boilerplate("skill", skill_name, skill_text))
        self.assertTrue(_is_ksa_definition_boilerplate("knowledge", knowledge_name, knowledge_text))
        self.assertTrue(_is_ksa_definition_boilerplate("knowledge", method_knowledge_name, method_knowledge_text))
        self.assertTrue(_is_ksa_definition_boilerplate("attitude", attitude_name, attitude_text))

    def test_promote_ksa_definitions_in_memory_contract(self) -> None:
        conn = self._memory_conn()
        boilerplate_concept_id, real_concept_id, _, real_text = self._seed_fixture(conn)
        timestamp = now_utc()
        conn.execute(
            """
            INSERT INTO ontology_concepts(
                concept_name, normalized_key, concept_type,
                definition, definition_source, definition_status,
                relation_status, review_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'defined', 'unlinked', 'human_reviewed', ?, ?)
            """,
            (
                "Human reviewed workforce attitude",
                "humanreviewedworkforceattitude",
                "attitude",
                "Human reviewed definition.",
                "manual",
                timestamp,
                timestamp,
            ),
        )
        human_concept_id = conn.execute(
            "SELECT concept_id FROM ontology_concepts WHERE normalized_key = 'humanreviewedworkforceattitude'"
        ).fetchone()["concept_id"]
        conn.execute(
            """
            INSERT INTO ksa_meaning_candidates(
                concept_id, concept_type, meaning_role, meaning_text,
                source_method, evidence_text, unit_code, element_id,
                criteria_id, ksa_id, confidence_score, review_status,
                created_at, updated_at
            ) VALUES (?, 'attitude', 'term_definition_candidate',
                      'Human reviewed workforce attitude: 협업 책임성을 유지하는 실질 정의 후보.',
                      'term_definition_template',
                      'fixture evidence', NULL, NULL, NULL, NULL, 0.95,
                      'llm_reviewed', ?, ?)
            """,
            (human_concept_id, timestamp, timestamp),
        )
        conn.commit()

        result = promote_ksa_definitions(conn, batch_size=1)

        boilerplate_row = conn.execute(
            """
            SELECT definition, definition_source, definition_status, review_status
            FROM ontology_concepts
            WHERE concept_id = ?
            """,
            (boilerplate_concept_id,),
        ).fetchone()
        real_row = conn.execute(
            """
            SELECT definition, definition_source, definition_status, review_status
            FROM ontology_concepts
            WHERE concept_id = ?
            """,
            (real_concept_id,),
        ).fetchone()
        human_row = conn.execute(
            """
            SELECT definition, definition_source, definition_status, review_status
            FROM ontology_concepts
            WHERE concept_id = ?
            """,
            (human_concept_id,),
        ).fetchone()
        conn.close()

        self.assertEqual(result, {"promoted": 1, "skipped_boilerplate": 1, "skipped_human_lock": 1})
        self.assertIsNone(boilerplate_row["definition"])
        self.assertIsNone(boilerplate_row["definition_source"])
        self.assertEqual(boilerplate_row["definition_status"], "missing")
        self.assertEqual(real_row["definition"], real_text)
        self.assertEqual(real_row["definition_status"], "candidate")
        self.assertEqual(real_row["definition_source"], "ksa_meaning_candidate_promotion")
        self.assertEqual(real_row["review_status"], "llm_reviewed")
        self.assertEqual(human_row["definition"], "Human reviewed definition.")
        self.assertEqual(human_row["definition_source"], "manual")
        self.assertEqual(human_row["definition_status"], "defined")
        self.assertEqual(human_row["review_status"], "human_reviewed")

    def test_promote_top_concepts_by_frequency_marks_auto_promoted(self) -> None:
        conn = self._memory_conn()
        boilerplate_concept_id, real_concept_id, _, real_text = self._seed_fixture(conn)
        self._seed_minimal_task_relation(conn, real_concept_id, real_concept_id)
        self._seed_minimal_task_relation(conn, boilerplate_concept_id, boilerplate_concept_id)

        result = promote_top_concepts_by_frequency(conn, top_n=2)

        boilerplate_row = conn.execute(
            """
            SELECT definition, definition_source, definition_status, review_status
            FROM ontology_concepts
            WHERE concept_id = ?
            """,
            (boilerplate_concept_id,),
        ).fetchone()
        real_row = conn.execute(
            """
            SELECT definition, definition_source, definition_status, review_status
            FROM ontology_concepts
            WHERE concept_id = ?
            """,
            (real_concept_id,),
        ).fetchone()
        conn.close()

        self.assertEqual(result["promoted"], 1)
        self.assertEqual(result["auto_promoted"], 1)
        self.assertEqual(result["skipped_boilerplate"], 1)
        self.assertEqual(result["skipped_human_lock"], 0)
        self.assertEqual(result["top_concepts_considered"], 2)
        self.assertIsNone(boilerplate_row["definition"])
        self.assertEqual(real_row["definition"], real_text)
        self.assertEqual(real_row["definition_source"], "ksa_meaning_candidate_promotion")
        self.assertEqual(real_row["definition_status"], "candidate")
        self.assertEqual(real_row["review_status"], "auto_promoted")

    def test_promote_top_concepts_by_frequency_uses_quality_filtered_priority(self) -> None:
        conn = self._memory_conn()
        noisy_concept_id = self._insert_concept(conn, "Noisy short KSA concept", concept_type="knowledge")
        timestamp = now_utc()
        conn.execute(
            """
            INSERT INTO ksa_meaning_candidates(
                concept_id, concept_type, meaning_role, meaning_text,
                source_method, evidence_text, unit_code, element_id,
                criteria_id, ksa_id, confidence_score, review_status,
                created_at, updated_at
            ) VALUES (?, 'knowledge', 'term_definition_candidate',
                      'Noisy short KSA concept: Specific non-boilerplate definition.',
                      'test', 'fixture evidence', NULL, NULL, NULL, NULL, 0.9,
                      'llm_reviewed', ?, ?)
            """,
            (noisy_concept_id, timestamp, timestamp),
        )
        self._seed_minimal_task_relation(conn, noisy_concept_id, noisy_concept_id)
        ksa_id = conn.execute(
            """
            SELECT kai.ksa_id
            FROM ksa_atomic_items kai
            JOIN ksa_atomic_concept_links kacl ON kacl.atomic_id = kai.atomic_id
            WHERE kacl.concept_id = ?
            LIMIT 1
            """,
            (noisy_concept_id,),
        ).fetchone()["ksa_id"]
        conn.execute(
            """
            INSERT INTO quality_issues(
                target_type, target_id, issue_type, severity,
                issue_detail, suggested_action, detected_at
            ) VALUES ('ksa', ?, 'short_ksa', 'info', 'short', 'review', ?)
            """,
            (str(ksa_id), timestamp),
        )
        conn.commit()

        result = promote_top_concepts_by_frequency(conn, top_n=10)
        row = conn.execute(
            "SELECT definition, definition_status, review_status FROM ontology_concepts WHERE concept_id = ?",
            (noisy_concept_id,),
        ).fetchone()
        conn.close()

        self.assertEqual(result["promoted"], 0)
        self.assertEqual(result["top_concepts_considered"], 0)
        self.assertIsNone(row["definition"])
        self.assertEqual(row["definition_status"], "missing")
        self.assertEqual(row["review_status"], "raw")

    def test_rank_concepts_by_recommendation_frequency_respects_quality_override(self) -> None:
        conn = self._memory_conn()
        low_frequency_id = self._insert_concept(conn, "Low frequency concept", concept_type="knowledge")
        high_frequency_id = self._insert_concept(conn, "High frequency concept", concept_type="skill")
        human_reviewed_id = self._insert_concept(
            conn,
            "Human reviewed concept",
            concept_type="attitude",
            review_status="human_reviewed",
        )
        accepted_id = self._insert_concept(
            conn,
            "Accepted concept",
            concept_type="knowledge",
            review_status="accepted",
        )
        reviewed_id = self._insert_concept(
            conn,
            "Reviewed concept",
            concept_type="skill",
            review_status="reviewed",
        )
        rejected_id = self._insert_concept(
            conn,
            "Rejected concept",
            concept_type="knowledge",
            review_status="rejected",
        )
        self._seed_minimal_task_relation(conn, low_frequency_id, low_frequency_id)
        self._seed_minimal_task_relation(conn, high_frequency_id, high_frequency_id)
        self._seed_minimal_task_relation(conn, high_frequency_id, high_frequency_id)
        self._seed_minimal_task_relation(conn, human_reviewed_id, human_reviewed_id)
        self._seed_minimal_task_relation(conn, accepted_id, accepted_id)
        self._seed_minimal_task_relation(conn, reviewed_id, reviewed_id)
        self._seed_minimal_task_relation(conn, rejected_id, rejected_id)
        timestamp = now_utc()
        for concept_id in (low_frequency_id, high_frequency_id):
            ksa_id = conn.execute(
                """
                SELECT kai.ksa_id
                FROM ksa_atomic_items kai
                JOIN ksa_atomic_concept_links kacl ON kacl.atomic_id = kai.atomic_id
                WHERE kacl.concept_id = ?
                LIMIT 1
                """,
                (concept_id,),
            ).fetchone()
            if ksa_id is None:
                atomic_row = conn.execute(
                    """
                    SELECT source_atomic_id
                    FROM task_ksa_concept_relations
                    WHERE source_concept_id = ?
                    LIMIT 1
                    """,
                    (concept_id,),
                ).fetchone()
                ksa_id = conn.execute(
                    "SELECT ksa_id FROM ksa_atomic_items WHERE atomic_id = ?",
                    (atomic_row["source_atomic_id"],),
                ).fetchone()
            conn.execute(
                """
                INSERT INTO quality_issues(
                    target_type, target_id, issue_type, severity,
                    issue_detail, suggested_action, detected_at
                ) VALUES ('ksa', ?, 'short_ksa', 'info', 'short', 'review', ?)
                """,
                (str(ksa_id["ksa_id"]), timestamp),
            )
        conn.commit()

        rows = rank_concepts_by_recommendation_frequency(conn, limit=10, high_frequency_override=4)
        conn.close()

        ranked_ids = [row["concept_id"] for row in rows]
        self.assertIn(high_frequency_id, ranked_ids)
        self.assertNotIn(low_frequency_id, ranked_ids)
        self.assertNotIn(human_reviewed_id, ranked_ids)
        self.assertNotIn(accepted_id, ranked_ids)
        self.assertNotIn(reviewed_id, ranked_ids)
        self.assertNotIn(rejected_id, ranked_ids)
        self.assertEqual(rows[0]["concept_id"], high_frequency_id)
        self.assertEqual(rows[0]["appearance_count"], 4)

    def test_rank_concepts_by_frequency_filters_duplicate_text_only_for_noncanonical_sources(self) -> None:
        conn = self._memory_conn()
        canonical_id = self._insert_concept(
            conn,
            "Duplicate text concept",
            concept_type="knowledge",
            normalized_key="duplicate_text_canonical",
        )
        source_id = self._insert_concept(
            conn,
            "Duplicate  text concept",
            concept_type="knowledge",
            normalized_key="duplicate_text_source",
        )
        self._seed_minimal_task_relation(conn, canonical_id, canonical_id)
        self._seed_minimal_task_relation(conn, source_id, source_id)
        build_duplicate_concept_relations(conn)
        timestamp = now_utc()
        for concept_id in (canonical_id, source_id):
            ksa_id = conn.execute(
                """
                SELECT kai.ksa_id
                FROM ksa_atomic_items kai
                JOIN ksa_atomic_concept_links kacl ON kacl.atomic_id = kai.atomic_id
                WHERE kacl.concept_id = ?
                LIMIT 1
                """,
                (concept_id,),
            ).fetchone()["ksa_id"]
            conn.execute(
                """
                INSERT INTO quality_issues(
                    target_type, target_id, issue_type, severity,
                    issue_detail, suggested_action, detected_at
                ) VALUES ('ksa', ?, 'duplicate_text', 'info', 'duplicate', 'review', ?)
                """,
                (str(ksa_id), timestamp),
            )
        conn.commit()

        rows = rank_concepts_by_recommendation_frequency(conn, limit=10, high_frequency_override=10)
        conn.close()

        ranked_ids = [row["concept_id"] for row in rows]
        self.assertIn(canonical_id, ranked_ids)
        self.assertNotIn(source_id, ranked_ids)

    def test_ksa_definition_priority_review_pack_adds_operator_context(self) -> None:
        conn = self._memory_conn()
        concept_id = self._insert_concept(
            conn,
            "Review pack concept",
            concept_type="knowledge",
            definition_status="candidate",
        )
        timestamp = now_utc()
        conn.execute(
            """
            UPDATE ontology_concepts
            SET definition = 'Review pack concept: 업무 판단과 문제 해결에 필요한 관련 원리, 기준, 절차, 사례에 대한 지식.',
                definition_source = 'ksa_meaning_candidates.term_definition_template'
            WHERE concept_id = ?
            """,
            (concept_id,),
        )
        conn.execute(
            """
            INSERT INTO ksa_meaning_candidates(
                concept_id, concept_type, meaning_role, meaning_text,
                source_method, evidence_text, unit_code, element_id,
                criteria_id, ksa_id, confidence_score, review_status,
                created_at, updated_at
            ) VALUES (?, 'knowledge', 'term_definition_candidate',
                      'Review pack concept: Specific evidence-backed candidate definition.',
                      'test', 'fixture evidence', NULL, NULL, NULL, NULL, 0.9,
                      'llm_reviewed', ?, ?)
            """,
            (concept_id, timestamp, timestamp),
        )
        self._seed_minimal_task_relation(conn, concept_id, concept_id)

        report = build_ksa_definition_priority_review_pack(
            conn,
            limit=5,
            high_frequency_override=0,
            evidence_limit=2,
        )
        with tempfile.TemporaryDirectory() as tmp:
            markdown_path = Path(tmp) / "review-pack.md"
            write_ksa_definition_priority_review_pack_markdown(report, markdown_path)
            markdown_text = markdown_path.read_text(encoding="utf-8")
        conn.close()

        self.assertEqual(report["schema"], "ncs_ksa_definition_priority_review_pack_v1")
        self.assertFalse(report["status_update_allowed"])
        self.assertEqual(report["row_count"], 1)
        row = report["rows"][0]
        self.assertEqual(row["concept_id"], concept_id)
        self.assertTrue(row["boilerplate_definition"])
        self.assertTrue(row["template_definition"])
        self.assertEqual(row["breadth"]["unit_count"], 1)
        self.assertEqual(row["relation_counts"]["source_count"], 1)
        self.assertEqual(row["relation_counts"]["target_count"], 1)
        self.assertEqual(row["term_definition_candidates"][0]["review_status"], "llm_reviewed")
        self.assertTrue(row["term_definition_candidates"][0]["promotion_eligible"])
        self.assertEqual(row["definition_candidate_screening"]["promotion_eligible_count"], 1)
        self.assertEqual(row["recommended_review_action"], "review_promotion_candidate")
        self.assertEqual(row["task_evidence_samples"][0]["criteria_text_raw"], "Criterion")
        draft = row["draft_definition_candidate"]
        self.assertEqual(draft["schema"], "ncs_ksa_definition_draft_candidate_v1")
        self.assertIn("Review pack concept", draft["draft_definition"])
        self.assertIn("Criterion", draft["draft_definition"])
        self.assertEqual(draft["review_policy"], "review_assist_only_not_a_human_decision")
        self.assertTrue(draft["human_decision_required"])
        self.assertFalse(draft["status_update_allowed"])
        self.assertFalse(draft["db_writes"])
        self.assertFalse(draft["approval_claim"])
        self.assertIn("definition_template_or_boilerplate", row["review_focus"])
        self.assertNotIn("no_promotable_definition_candidate", row["review_focus"])
        self.assertIn("KSA Definition Priority Review Pack", markdown_text)
        self.assertIn("Draft definition candidate", markdown_text)
        self.assertIn("review_assist_only_not_a_human_decision", markdown_text)
        self.assertIn("eligible=True", markdown_text)

    def test_ksa_definition_priority_review_pack_flags_template_only_candidates(self) -> None:
        conn = self._memory_conn()
        concept_id = self._insert_concept(
            conn,
            "Template only concept",
            concept_type="skill",
            definition_status="candidate",
        )
        timestamp = now_utc()
        meaning_text = _term_definition_text_for_concept(
            {"concept_name": "Template only concept", "concept_type": "skill"}
        )
        conn.execute(
            """
            INSERT INTO ksa_meaning_candidates(
                concept_id, concept_type, meaning_role, meaning_text,
                source_method, evidence_text, unit_code, element_id,
                criteria_id, ksa_id, confidence_score, review_status,
                created_at, updated_at
            ) VALUES (?, 'skill', 'term_definition_candidate',
                      ?, 'term_definition_template',
                      'fixture evidence', NULL, NULL, NULL, NULL, 0.9,
                      'llm_reviewed', ?, ?)
            """,
            (concept_id, meaning_text, timestamp, timestamp),
        )
        self._seed_minimal_task_relation(conn, concept_id, concept_id)

        report = build_ksa_definition_priority_review_pack(
            conn,
            limit=5,
            high_frequency_override=0,
            evidence_limit=2,
        )
        conn.close()

        row = report["rows"][0]
        self.assertEqual(row["concept_id"], concept_id)
        self.assertEqual(row["definition_candidate_screening"]["candidate_count"], 1)
        self.assertEqual(row["definition_candidate_screening"]["promotion_eligible_count"], 0)
        self.assertEqual(row["definition_candidate_screening"]["boilerplate_candidate_count"], 1)
        self.assertFalse(row["term_definition_candidates"][0]["promotion_eligible"])
        self.assertEqual(
            row["term_definition_candidates"][0]["promotion_block_reason"],
            "generated_template_boilerplate",
        )
        self.assertEqual(row["recommended_review_action"], "draft_for_human_review_only")
        self.assertIn("no_promotable_definition_candidate", row["review_focus"])
        self.assertEqual(
            row["draft_definition_candidate"]["review_policy"],
            "review_assist_only_not_a_human_decision",
        )
        self.assertFalse(row["draft_definition_candidate"]["db_writes"])

    def test_ksa_definition_priority_review_pack_surfaces_ksa_targeted_quality_issues(self) -> None:
        conn = self._memory_conn()
        concept_id = self._insert_concept(conn, "Quality issue concept", concept_type="knowledge")
        canonical_id = self._insert_concept(conn, "Quality issue canonical", concept_type="knowledge")
        timestamp = now_utc()
        conn.execute(
            """
            INSERT INTO ontology_concept_relations(
                source_concept_id, relation_type, target_concept_id,
                relation_label, review_status, created_at
            ) VALUES (?, 'same_as', ?, 'duplicate_normalized_key', 'candidate', ?)
            """,
            (concept_id, canonical_id, timestamp),
        )
        self._seed_minimal_task_relation(conn, concept_id, canonical_id)
        ksa_id = conn.execute("SELECT MAX(ksa_id) AS ksa_id FROM ksa_items").fetchone()["ksa_id"]
        for issue_type in ("short_ksa", "duplicate_text"):
            conn.execute(
                """
                INSERT INTO quality_issues(
                    target_type, target_id, issue_type, severity, issue_detail,
                    suggested_action, detected_at
                ) VALUES ('ksa', ?, ?, 'info', 'fixture issue', 'review', ?)
                """,
                (str(ksa_id), issue_type, timestamp),
            )
        conn.commit()

        report = build_ksa_definition_priority_review_pack(
            conn,
            limit=5,
            high_frequency_override=0,
            evidence_limit=2,
        )
        conn.close()

        row = next(item for item in report["rows"] if item["concept_id"] == concept_id)
        self.assertEqual(row["quality_issue_counts"], {"duplicate_text": 1, "short_ksa": 1})
        self.assertIn("quality_issue_present", row["review_focus"])
        self.assertFalse(row["status_update_allowed"])
        self.assertFalse(report["status_update_allowed"])

    def test_build_duplicate_concept_relations_dry_run_and_canonical_selection(self) -> None:
        conn = self._memory_conn()
        low_id = self._insert_concept(
            conn,
            "Duplicate Concept",
            concept_type="knowledge",
            normalized_key="duplicate_concept_low",
        )
        canonical_id = self._insert_concept(
            conn,
            "Duplicate  Concept",
            concept_type="knowledge",
            normalized_key="duplicate_concept_canonical",
        )
        other_id = self._insert_concept(
            conn,
            "duplicate concept",
            concept_type="knowledge",
            normalized_key="duplicate_concept_other",
        )
        cross_type_id = self._insert_concept(
            conn,
            "Duplicate Concept",
            concept_type="skill",
            normalized_key="duplicate_concept_skill",
        )
        self._seed_minimal_task_relation(conn, canonical_id, canonical_id)
        self._seed_minimal_task_relation(conn, canonical_id, canonical_id)
        self._seed_minimal_task_relation(conn, low_id, low_id)
        self._seed_minimal_task_relation(conn, cross_type_id, cross_type_id)

        dry_run = build_duplicate_concept_relations(conn, dry_run=True)
        relation_count_after_dry_run = conn.execute(
            "SELECT COUNT(*) AS count FROM ontology_concept_relations WHERE relation_type = 'same_as'"
        ).fetchone()["count"]

        actual = build_duplicate_concept_relations(conn)
        rows = conn.execute(
            """
            SELECT source_concept_id, target_concept_id, relation_label, review_status
            FROM ontology_concept_relations
            WHERE relation_type = 'same_as'
            ORDER BY source_concept_id
            """
        ).fetchall()
        second_run = build_duplicate_concept_relations(conn)
        conn.close()

        self.assertEqual(dry_run, {"groups_found": 1, "pairs_inserted": 2})
        self.assertEqual(relation_count_after_dry_run, 0)
        self.assertEqual(actual, {"groups_found": 1, "pairs_inserted": 2})
        self.assertEqual(second_run, {"groups_found": 1, "pairs_inserted": 0})
        self.assertEqual({row["source_concept_id"] for row in rows}, {low_id, other_id})
        self.assertEqual({row["target_concept_id"] for row in rows}, {canonical_id})
        self.assertNotIn(cross_type_id, {row["source_concept_id"] for row in rows})
        self.assertNotIn(cross_type_id, {row["target_concept_id"] for row in rows})
        self.assertEqual({row["relation_label"] for row in rows}, {"duplicate_normalized_key"})
        self.assertEqual({row["review_status"] for row in rows}, {"candidate"})

    def test_build_duplicate_concept_relations_prefers_trusted_canonical_over_frequency(self) -> None:
        conn = self._memory_conn()
        trusted_id = self._insert_concept(
            conn,
            "Trusted Duplicate",
            concept_type="knowledge",
            normalized_key="trusted_duplicate_manual",
            review_status="human_reviewed",
            definition_status="defined",
        )
        frequent_id = self._insert_concept(
            conn,
            "Trusted  Duplicate",
            concept_type="knowledge",
            normalized_key="trusted_duplicate_frequent",
        )
        self._seed_minimal_task_relation(conn, frequent_id, frequent_id)
        self._seed_minimal_task_relation(conn, frequent_id, frequent_id)
        self._seed_minimal_task_relation(conn, trusted_id, trusted_id)

        result = build_duplicate_concept_relations(conn)
        row = conn.execute(
            """
            SELECT source_concept_id, target_concept_id
            FROM ontology_concept_relations
            WHERE relation_type = 'same_as'
            """
        ).fetchone()
        conn.close()

        self.assertEqual(result, {"groups_found": 1, "pairs_inserted": 1})
        self.assertEqual(row["source_concept_id"], frequent_id)
        self.assertEqual(row["target_concept_id"], trusted_id)

    def test_build_duplicate_concept_relations_reconciles_canonical_change(self) -> None:
        conn = self._memory_conn()
        first_id = self._insert_concept(
            conn,
            "Flip Duplicate",
            concept_type="knowledge",
            normalized_key="flip_duplicate_first",
        )
        second_id = self._insert_concept(
            conn,
            "Flip  Duplicate",
            concept_type="knowledge",
            normalized_key="flip_duplicate_second",
        )
        self._seed_minimal_task_relation(conn, first_id, first_id)
        self._seed_minimal_task_relation(conn, first_id, first_id)
        self._seed_minimal_task_relation(conn, second_id, second_id)

        first_run = build_duplicate_concept_relations(conn)
        conn.execute(
            """
            UPDATE ontology_concepts
            SET review_status = 'human_reviewed',
                definition_status = 'defined'
            WHERE concept_id = ?
            """,
            (second_id,),
        )
        second_run = build_duplicate_concept_relations(conn)
        rows = conn.execute(
            """
            SELECT source_concept_id, target_concept_id, review_status
            FROM ontology_concept_relations
            WHERE relation_type = 'same_as'
            ORDER BY relation_id
            """
        ).fetchall()
        conn.close()

        active_pairs = {
            (row["source_concept_id"], row["target_concept_id"])
            for row in rows
            if row["review_status"] != "rejected"
        }
        rejected_pairs = {
            (row["source_concept_id"], row["target_concept_id"])
            for row in rows
            if row["review_status"] == "rejected"
        }
        self.assertEqual(first_run, {"groups_found": 1, "pairs_inserted": 1})
        self.assertEqual(second_run, {"groups_found": 1, "pairs_inserted": 1})
        self.assertEqual(active_pairs, {(first_id, second_id)})
        self.assertEqual(rejected_pairs, {(second_id, first_id)})

    def test_harness_ksa_definition_priority_report_writes_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            out_path = Path(tmp) / "priority.json"
            conn = connect(db_path)
            initialize_database(conn)
            concept_id = self._insert_concept(conn, "Priority concept", concept_type="knowledge")
            reviewed_id = self._insert_concept(
                conn,
                "Reviewed priority concept",
                concept_type="skill",
                review_status="accepted",
            )
            self._seed_minimal_task_relation(conn, concept_id, concept_id)
            self._seed_minimal_task_relation(conn, concept_id, concept_id)
            self._seed_minimal_task_relation(conn, reviewed_id, reviewed_id)
            conn.close()

            env = os.environ.copy()
            env["NCS_DB_PATH"] = str(db_path)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(HARNESS),
                    "ksa-definition-priority-report",
                    "--limit",
                    "5",
                    "--out",
                    str(out_path),
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            stdout_payload = json.loads(proc.stdout)
            report = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(stdout_payload["status"], "report_written")
            self.assertTrue(stdout_payload["report_only"])
            self.assertFalse(stdout_payload["status_update_allowed"])
            self.assertFalse(stdout_payload["db_writes"])
            self.assertFalse(stdout_payload["approval_claim"])
            self.assertEqual(report["schema"], "ncs_ksa_definition_priority_report_v1")
            self.assertTrue(report["report_only"])
            self.assertFalse(report["status_update_allowed"])
            self.assertFalse(report["db_writes"])
            self.assertFalse(report["approval_claim"])
            self.assertTrue(report["human_decision_required_for_approval"])
            self.assertEqual(
                report["filters"]["excluded_review_statuses"],
                ["human_reviewed", "accepted", "reviewed", "rejected"],
            )
            self.assertEqual([row["concept_id"] for row in report["rows"]], [concept_id])
            self.assertEqual(report["rows"][0]["appearance_count"], 4)

    def test_harness_ksa_definition_priority_review_pack_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            out_path = Path(tmp) / "priority_pack.json"
            markdown_path = Path(tmp) / "priority_pack.md"
            csv_path = Path(tmp) / "priority_pack.csv"
            conn = connect(db_path)
            initialize_database(conn)
            concept_id = self._insert_concept(conn, " @Harness review pack concept", concept_type="knowledge")
            self._seed_minimal_task_relation(conn, concept_id, concept_id)
            conn.close()

            env = os.environ.copy()
            env["NCS_DB_PATH"] = str(db_path)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(HARNESS),
                    "ksa-definition-priority-review-pack",
                    "--limit",
                    "5",
                    "--out",
                    str(out_path),
                    "--markdown-out",
                    str(markdown_path),
                    "--csv-out",
                    str(csv_path),
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            stdout_payload = json.loads(proc.stdout)
            report = json.loads(out_path.read_text(encoding="utf-8"))
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual(stdout_payload["status"], "report_written")
            self.assertEqual(report["schema"], "ncs_ksa_definition_priority_review_pack_v1")
            self.assertFalse(report["status_update_allowed"])
            self.assertFalse(report["db_writes"])
            self.assertFalse(report["approval_claim"])
            self.assertEqual(stdout_payload["csv_out"], str(csv_path))
            self.assertEqual(stdout_payload["csv_record_count"], 1)
            self.assertEqual(stdout_payload["csv_decision_blank_count"], 1)
            self.assertEqual(report["rows"][0]["concept_id"], concept_id)
            self.assertFalse(report["rows"][0]["status_update_allowed"])
            self.assertTrue(markdown_path.exists())
            self.assertTrue(csv_path.exists())
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("db_writes: `False`", markdown)
            self.assertIn("approval_claim: `False`", markdown)
            self.assertEqual(len(csv_rows), 1)
            self.assertEqual(csv_rows[0]["schema"], "ncs_ksa_definition_priority_review_decision_row_v1")
            self.assertEqual(csv_rows[0]["concept_id"], str(concept_id))
            self.assertEqual(csv_rows[0]["concept_name"], "' @Harness review pack concept")
            self.assertEqual(csv_rows[0]["decision"], "")
            self.assertEqual(csv_rows[0]["approved_definition"], "")
            self.assertEqual(csv_rows[0]["reviewer_id"], "")
            self.assertEqual(csv_rows[0]["draft_review_policy"], "review_assist_only_not_a_human_decision")
            self.assertEqual(csv_rows[0]["status_update_allowed"], "False")
            self.assertEqual(csv_rows[0]["db_writes"], "False")
            self.assertEqual(csv_rows[0]["approval_claim"], "False")

    def test_harness_ksa_definition_review_operator_packet_writes_readonly_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            out_path = Path(tmp) / "definition_operator_packet.json"
            markdown_path = Path(tmp) / "definition_operator_packet.md"
            conn = connect(db_path)
            initialize_database(conn)
            concept_id = self._insert_concept(conn, "Harness definition packet", concept_type="knowledge")
            self._seed_minimal_task_relation(conn, concept_id, concept_id)
            conn.close()

            env = os.environ.copy()
            env["NCS_DB_PATH"] = str(db_path)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(HARNESS),
                    "ksa-definition-review-operator-packet",
                    "--limit",
                    "5",
                    "--out",
                    str(out_path),
                    "--markdown-out",
                    str(markdown_path),
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            stdout_payload = json.loads(proc.stdout)
            packet = json.loads(out_path.read_text(encoding="utf-8"))
            csv_path = Path(packet["artifacts"]["priority_review_csv"])
            review_pack_path = Path(packet["artifacts"]["priority_review_pack"])
            promotion_path = Path(packet["artifacts"]["promotion_status"])
            priority_path = Path(packet["artifacts"]["priority_report"])
            decision_audit_path = Path(packet["artifacts"]["decision_audit"])
            action_plan_path = Path(packet["artifacts"]["action_plan"])
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            decision_audit = json.loads(decision_audit_path.read_text(encoding="utf-8"))
            action_plan = json.loads(action_plan_path.read_text(encoding="utf-8"))

            self.assertEqual(stdout_payload["schema"], "ncs_ksa_definition_review_operator_packet_v1")
            self.assertEqual(packet["schema"], "ncs_ksa_definition_review_operator_packet_v1")
            self.assertTrue(packet["ok"])
            self.assertTrue(packet["human_decision_required"])
            self.assertFalse(packet["status_update_allowed"])
            self.assertFalse(packet["db_writes"])
            self.assertFalse(packet["approval_claim"])
            self.assertFalse(packet["trusted_status_write_allowed"])
            self.assertFalse(packet["raw_source_mutation_allowed"])
            self.assertFalse(packet["source_payload_exposed"])
            self.assertEqual(packet["summary"]["review_pack_row_count"], 1)
            self.assertEqual(packet["summary"]["review_csv_record_count"], 1)
            self.assertEqual(packet["summary"]["decision_blank_count"], 1)
            self.assertEqual(packet["summary"]["pending_decision_count"], 1)
            self.assertEqual(packet["summary"]["action_plan_action_count"], 0)
            self.assertEqual(
                packet["summary"]["draft_policy_counts"],
                {"review_assist_only_not_a_human_decision": 1},
            )
            self.assertEqual(packet["summary"]["first_review_queue"][0]["concept_id"], concept_id)
            self.assertEqual(stdout_payload["priority_review_csv"], str(csv_path))
            self.assertTrue(markdown_path.exists())
            self.assertTrue(review_pack_path.exists())
            self.assertTrue(promotion_path.exists())
            self.assertTrue(priority_path.exists())
            self.assertTrue(decision_audit_path.exists())
            self.assertTrue(action_plan_path.exists())
            self.assertEqual(decision_audit["schema"], "ncs_ksa_definition_review_decision_audit_v1")
            self.assertEqual(decision_audit["pending_decision_count"], 1)
            self.assertEqual(decision_audit["action_eligible_count"], 0)
            self.assertFalse(decision_audit["db_writes"])
            self.assertEqual(action_plan["schema"], "ncs_ksa_definition_review_action_plan_v1")
            self.assertEqual(action_plan["action_count"], 0)
            self.assertFalse(action_plan["db_writes"])
            self.assertEqual(len(csv_rows), 1)
            self.assertEqual(csv_rows[0]["decision"], "")
            self.assertEqual(csv_rows[0]["approved_definition"], "")
            self.assertEqual(csv_rows[0]["reviewer_id"], "")
            self.assertEqual(csv_rows[0]["status_update_allowed"], "False")
            self.assertEqual(csv_rows[0]["db_writes"], "False")
            self.assertEqual(csv_rows[0]["approval_claim"], "False")
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("KSA Definition Review Operator Packet", markdown)
            self.assertIn("db_writes: `False`", markdown)

    def test_harness_ksa_definition_review_audit_and_action_plan_are_readonly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            packet_path = Path(tmp) / "definition_operator_packet.json"
            audit_path = Path(tmp) / "definition_decision_audit.json"
            audit_markdown_path = Path(tmp) / "definition_decision_audit.md"
            plan_path = Path(tmp) / "definition_action_plan.json"
            plan_markdown_path = Path(tmp) / "definition_action_plan.md"
            conn = connect(db_path)
            initialize_database(conn)
            concept_id = self._insert_concept(conn, "Harness audited definition", concept_type="skill")
            self._seed_minimal_task_relation(conn, concept_id, concept_id)
            conn.close()

            env = os.environ.copy()
            env["NCS_DB_PATH"] = str(db_path)
            packet_proc = subprocess.run(
                [
                    sys.executable,
                    str(HARNESS),
                    "ksa-definition-review-operator-packet",
                    "--limit",
                    "5",
                    "--out",
                    str(packet_path),
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(packet_proc.returncode, 0, packet_proc.stdout + packet_proc.stderr)
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            csv_path = Path(packet["artifacts"]["priority_review_csv"])
            review_pack_path = Path(packet["artifacts"]["priority_review_pack"])

            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                fieldnames = reader.fieldnames or []
            rows[0]["decision"] = "approve_definition"
            rows[0]["approved_definition"] = "현장 절차와 도구를 적용하여 과업을 안정적으로 수행하는 능력."
            rows[0]["reviewer_id"] = "human-reviewer-1"
            rows[0]["reviewed_at"] = "2026-06-26T00:00:00Z"
            rows[0]["rationale"] = "수행준거 근거와 개념명이 일치함."
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            audit_proc = subprocess.run(
                [
                    sys.executable,
                    str(HARNESS),
                    "audit-ksa-definition-review-decisions",
                    "--csv",
                    str(csv_path),
                    "--source-packet",
                    str(packet_path),
                    "--source-review-pack",
                    str(review_pack_path),
                    "--out",
                    str(audit_path),
                    "--markdown-out",
                    str(audit_markdown_path),
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(audit_proc.returncode, 0, audit_proc.stdout + audit_proc.stderr)
            plan_proc = subprocess.run(
                [
                    sys.executable,
                    str(HARNESS),
                    "plan-ksa-definition-review-actions",
                    "--csv",
                    str(csv_path),
                    "--source-packet",
                    str(packet_path),
                    "--source-review-pack",
                    str(review_pack_path),
                    "--out",
                    str(plan_path),
                    "--markdown-out",
                    str(plan_markdown_path),
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(plan_proc.returncode, 0, plan_proc.stdout + plan_proc.stderr)
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(audit["schema"], "ncs_ksa_definition_review_decision_audit_v1")
            self.assertEqual(audit["completed_decision_count"], 1)
            self.assertEqual(audit["pending_decision_count"], 0)
            self.assertEqual(audit["action_eligible_count"], 1)
            self.assertTrue(audit["report_only"])
            self.assertFalse(audit["status_update_allowed"])
            self.assertFalse(audit["db_writes"])
            self.assertFalse(audit["api_calls"])
            self.assertFalse(audit["acceptance_claim"])
            self.assertEqual(plan["schema"], "ncs_ksa_definition_review_action_plan_v1")
            self.assertEqual(plan["action_count"], 1)
            self.assertTrue(plan["actions"][0]["requires_explicit_operator_apply"])
            self.assertEqual(plan["actions"][0]["target_fields"]["review_status"], "human_reviewed")
            self.assertTrue(plan["report_only"])
            self.assertFalse(plan["status_update_allowed"])
            self.assertFalse(plan["db_writes"])
            self.assertFalse(plan["api_calls"])
            self.assertFalse(plan["acceptance_claim"])
            self.assertTrue(audit_markdown_path.exists())
            self.assertTrue(plan_markdown_path.exists())

    def test_harness_build_duplicate_concept_relations_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            canonical_id = self._insert_concept(
                conn,
                "Harness Duplicate",
                concept_type="knowledge",
                normalized_key="harness_duplicate_canonical",
            )
            duplicate_id = self._insert_concept(
                conn,
                "Harness  Duplicate",
                concept_type="knowledge",
                normalized_key="harness_duplicate_source",
            )
            self._seed_minimal_task_relation(conn, canonical_id, canonical_id)
            self._seed_minimal_task_relation(conn, canonical_id, canonical_id)
            self._seed_minimal_task_relation(conn, duplicate_id, duplicate_id)
            conn.execute(
                "UPDATE ncs_query_aliases SET unit_code = '0202020102_23v3' WHERE alias_text IN (?, ?)",
                ("\ucc44\uc6a9", "\uc778\ub825\ucc44\uc6a9"),
            )
            old_alias_count = conn.execute(
                "SELECT COUNT(*) AS count FROM ncs_query_aliases WHERE unit_code = '0202020102_23v3'"
            ).fetchone()["count"]
            self.assertGreater(old_alias_count, 0)
            conn.commit()
            conn.close()

            env = os.environ.copy()
            env["NCS_DB_PATH"] = str(db_path)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(HARNESS),
                    "build-duplicate-concept-relations",
                    "--dry-run",
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["groups_found"], 1)
            self.assertEqual(payload["pairs_inserted"], 1)
            conn = connect(db_path)
            relation_count = conn.execute(
                "SELECT COUNT(*) AS count FROM ontology_concept_relations WHERE relation_type = 'same_as'"
            ).fetchone()["count"]
            old_alias_count_after = conn.execute(
                "SELECT COUNT(*) AS count FROM ncs_query_aliases WHERE unit_code = '0202020102_23v3'"
            ).fetchone()["count"]
            conn.close()
            self.assertEqual(relation_count, 0)
            self.assertEqual(old_alias_count_after, old_alias_count)

    def test_promote_ksa_definitions_skips_boilerplate_and_promotes_real_definition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            boilerplate_concept_id, real_concept_id, _, real_text = self._seed_fixture(conn)

            result = promote_ksa_definitions(conn, batch_size=1)

            boilerplate_row = conn.execute(
                """
                SELECT definition, definition_source, definition_status, review_status
                FROM ontology_concepts
                WHERE concept_id = ?
                """,
                (boilerplate_concept_id,),
            ).fetchone()
            real_row = conn.execute(
                """
                SELECT definition, definition_source, definition_status, review_status
                FROM ontology_concepts
                WHERE concept_id = ?
                """,
                (real_concept_id,),
            ).fetchone()
            conn.close()

            self.assertEqual(result["promoted"], 1)
            self.assertEqual(result["skipped_boilerplate"], 1)
            self.assertEqual(result["skipped_human_lock"], 0)
            self.assertIsNone(boilerplate_row["definition"])
            self.assertIsNone(boilerplate_row["definition_source"])
            self.assertEqual(boilerplate_row["definition_status"], "missing")
            self.assertEqual(boilerplate_row["review_status"], "raw")
            self.assertEqual(
                real_row["definition"],
                "Workforce planning skill: 인력 수요를 분석하고 실행 계획을 수립하는 기술.",
            )
            self.assertEqual(real_row["definition_source"], "ksa_meaning_candidate_promotion")
            self.assertEqual(real_row["definition_status"], "candidate")
            self.assertEqual(real_row["review_status"], "llm_reviewed")

    def test_promote_ksa_definitions_skips_generated_template_variants(self) -> None:
        conn = self._memory_conn()
        timestamp = now_utc()
        fixture_rows = [
            ("Workforce analysis", "knowledge"),
            ("Workforce execution", "skill"),
        ]
        for concept_name, concept_type in fixture_rows:
            concept_id = self._insert_concept(conn, concept_name, concept_type=concept_type)
            meaning_text = _term_definition_text_for_concept(
                {"concept_name": concept_name, "concept_type": concept_type}
            )
            conn.execute(
                """
                INSERT INTO ksa_meaning_candidates(
                    concept_id, concept_type, meaning_role, meaning_text,
                    source_method, evidence_text, unit_code, element_id,
                    criteria_id, ksa_id, confidence_score, review_status,
                    created_at, updated_at
                ) VALUES (?, ?, 'term_definition_candidate', ?,
                          'term_definition_template',
                          'fixture evidence', NULL, NULL, NULL, NULL, 0.9,
                          'llm_reviewed', ?, ?)
                """,
                (concept_id, concept_type, meaning_text, timestamp, timestamp),
            )
        conn.commit()

        result = promote_ksa_definitions(conn, batch_size=1)
        rows = conn.execute(
            """
            SELECT definition, definition_status, review_status
            FROM ontology_concepts
            WHERE concept_name IN ('Workforce analysis', 'Workforce execution')
            ORDER BY concept_name
            """
        ).fetchall()
        conn.close()

        self.assertEqual(result["promoted"], 0)
        self.assertEqual(result["skipped_boilerplate"], 2)
        self.assertTrue(all(row["definition"] is None for row in rows))
        self.assertTrue(all(row["definition_status"] == "missing" for row in rows))
        self.assertTrue(all(row["review_status"] == "raw" for row in rows))

    def test_ksa_definition_promotion_status_reports_promotable_and_locked_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            boilerplate_concept_id, real_concept_id, _, real_text = self._seed_fixture(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type,
                    definition, definition_source, definition_status,
                    relation_status, review_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'defined', 'unlinked', 'human_reviewed', ?, ?)
                """,
                (
                    "Locked workforce planning attitude",
                    "lockedworkforceplanningattitude",
                    "attitude",
                    "Human-authored locked definition.",
                    "manual",
                    timestamp,
                    timestamp,
                ),
            )
            locked_concept_id = conn.execute(
                "SELECT concept_id FROM ontology_concepts WHERE normalized_key = 'lockedworkforceplanningattitude'"
            ).fetchone()["concept_id"]
            conn.execute(
                """
                INSERT INTO ksa_meaning_candidates(
                    concept_id, concept_type, meaning_role, meaning_text,
                    source_method, evidence_text, unit_code, element_id,
                    criteria_id, ksa_id, confidence_score, review_status,
                    created_at, updated_at
                ) VALUES (?, 'attitude', 'term_definition_candidate',
                          'Locked workforce planning attitude: ?ㅽ뻾 怨꾪쉷?섍린 ?뚯븙???꾪븳 踰꾪슚???덉쑝??',
                          'term_definition_template',
                          'fixture evidence',
                          NULL, NULL, NULL, NULL, 0.90,
                          'llm_reviewed', ?, ?)
                """,
                (locked_concept_id, timestamp, timestamp),
            )
            report = ksa_definition_promotion_status(conn, batch_size=1, sample_limit=5)
            conn.close()

        self.assertEqual(report["candidate_rows_scanned"], 3)
        self.assertEqual(report["promotable"], 1)
        self.assertEqual(report["skipped_boilerplate"], 1)
        self.assertEqual(report["skipped_human_lock"], 1)
        self.assertEqual(report["promotable_by_concept_type"], {"skill": 1})
        self.assertEqual(report["skipped_boilerplate_by_concept_type"], {"knowledge": 1})
        self.assertEqual(report["skipped_human_lock_by_concept_type"], {"attitude": 1})
        self.assertIn("generated_template_sample_names", report["criteria"])
        self.assertEqual(report["criteria"]["generated_template_body_counts"]["knowledge"], 2)
        self.assertEqual(report["criteria"]["generated_template_body_counts"]["skill"], 2)
        self.assertEqual(report["samples"]["promotable"][0]["concept_id"], real_concept_id)
        self.assertEqual(report["samples"]["promotable"][0]["reason"], "promotable")
        self.assertEqual(report["samples"]["skipped_boilerplate"][0]["concept_id"], boilerplate_concept_id)
        self.assertEqual(report["samples"]["skipped_boilerplate"][0]["reason"], "boilerplate_prefix")
        self.assertEqual(report["samples"]["skipped_human_lock"][0]["concept_id"], locked_concept_id)
        self.assertEqual(report["samples"]["skipped_human_lock"][0]["reason"], "human_lock")
        self.assertEqual(report["samples"]["promotable"][0]["meaning_text"], real_text)

    def test_harness_promote_definitions_flag_runs_independently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            boilerplate_concept_id, real_concept_id, _, real_text = self._seed_fixture(conn)
            conn.close()

            env = os.environ.copy()
            env["NCS_DB_PATH"] = str(db_path)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(HARNESS),
                    "preprocess-ncs-ontology",
                    "--no-relations",
                    "--promote-definitions",
                    "--approve-ksa-definition-promotion",
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["definition_promotions"]["promoted"], 1)
            self.assertEqual(payload["definition_promotions"]["skipped_boilerplate"], 1)
            self.assertEqual(payload["definition_promotions"]["skipped_human_lock"], 0)

            conn = connect(db_path)
            boilerplate_row = conn.execute(
                "SELECT definition, definition_source, definition_status, review_status FROM ontology_concepts WHERE concept_id = ?",
                (boilerplate_concept_id,),
            ).fetchone()
            real_row = conn.execute(
                "SELECT definition, definition_source, definition_status, review_status FROM ontology_concepts WHERE concept_id = ?",
                (real_concept_id,),
            ).fetchone()
            conn.close()

            self.assertIsNone(boilerplate_row["definition"])
            self.assertIsNone(boilerplate_row["definition_source"])
            self.assertEqual(boilerplate_row["definition_status"], "missing")
            self.assertEqual(real_row["definition"], real_text)
            self.assertEqual(real_row["definition_source"], "ksa_meaning_candidate_promotion")
            self.assertEqual(real_row["definition_status"], "candidate")
            self.assertEqual(real_row["review_status"], "llm_reviewed")

    def test_harness_promote_definitions_requires_explicit_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            _, real_concept_id, _, _ = self._seed_fixture(conn)
            conn.close()

            env = os.environ.copy()
            env["NCS_DB_PATH"] = str(db_path)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(HARNESS),
                    "preprocess-ncs-ontology",
                    "--no-relations",
                    "--promote-definitions",
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"], "ksa_definition_promotion_requires_approval")
            self.assertIn("report-ksa-definition-promotion", payload["dry_run_command"])

            conn = connect(db_path)
            real_row = conn.execute(
                "SELECT definition, definition_source, definition_status FROM ontology_concepts WHERE concept_id = ?",
                (real_concept_id,),
            ).fetchone()
            conn.close()

            self.assertIsNone(real_row["definition"])
            self.assertIsNone(real_row["definition_source"])
            self.assertEqual(real_row["definition_status"], "missing")

    def test_promote_ksa_definitions_preserves_locked_concepts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            timestamp = now_utc()

            conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type,
                    definition, definition_source, definition_status,
                    relation_status, review_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'defined', 'unlinked', 'raw', ?, ?)
                """,
                (
                    "Locked workforce planning knowledge",
                    "lockedworkforceplanningknowledge",
                    "knowledge",
                    "Human-authored locked definition.",
                    "manual",
                    timestamp,
                    timestamp,
                ),
            )
            conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type,
                    definition, definition_source, definition_status,
                    relation_status, review_status, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, NULL, 'missing', 'unlinked', 'human_reviewed', ?, ?)
                """,
                (
                    "Reviewed workforce planning skill",
                    "reviewedworkforceplanningskill",
                    "skill",
                    timestamp,
                    timestamp,
                ),
            )

            locked_defined_id = conn.execute(
                "SELECT concept_id FROM ontology_concepts WHERE normalized_key = 'lockedworkforceplanningknowledge'"
            ).fetchone()["concept_id"]
            locked_reviewed_id = conn.execute(
                "SELECT concept_id FROM ontology_concepts WHERE normalized_key = 'reviewedworkforceplanningskill'"
            ).fetchone()["concept_id"]

            conn.execute(
                """
                INSERT INTO ksa_meaning_candidates(
                    concept_id, concept_type, meaning_role, meaning_text,
                    source_method, evidence_text, unit_code, element_id,
                    criteria_id, ksa_id, confidence_score, review_status,
                    created_at, updated_at
                ) VALUES (?, 'knowledge', 'term_definition_candidate',
                          'Locked workforce planning knowledge: 업무 판단과 문제 해결에 필요한 지식.',
                          'term_definition_template',
                          'fixture evidence',
                          NULL, NULL, NULL, NULL, 0.91,
                          'llm_reviewed', ?, ?)
                """,
                (locked_defined_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO ksa_meaning_candidates(
                    concept_id, concept_type, meaning_role, meaning_text,
                    source_method, evidence_text, unit_code, element_id,
                    criteria_id, ksa_id, confidence_score, review_status,
                    created_at, updated_at
                ) VALUES (?, 'skill', 'term_definition_candidate',
                          'Reviewed workforce planning skill: 인력 수요를 분석하고 실행 계획을 수립하는 기술.',
                          'term_definition_template',
                          'fixture evidence',
                          NULL, NULL, NULL, NULL, 0.93,
                          'llm_reviewed', ?, ?)
                """,
                (locked_reviewed_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type,
                    definition, definition_source, definition_status,
                    relation_status, review_status, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, NULL, 'missing', 'unlinked', 'rejected', ?, ?)
                """,
                (
                    "Rejected workforce planning attitude",
                    "rejectedworkforceplanningattitude",
                    "attitude",
                    timestamp,
                    timestamp,
                ),
            )
            rejected_id = conn.execute(
                "SELECT concept_id FROM ontology_concepts WHERE normalized_key = 'rejectedworkforceplanningattitude'"
            ).fetchone()["concept_id"]
            conn.execute(
                """
                INSERT INTO ksa_meaning_candidates(
                    concept_id, concept_type, meaning_role, meaning_text,
                    source_method, evidence_text, unit_code, element_id,
                    criteria_id, ksa_id, confidence_score, review_status,
                    created_at, updated_at
                ) VALUES (?, 'attitude', 'term_definition_candidate',
                          'Rejected workforce planning attitude: Specific rejected definition candidate.',
                          'term_definition_template',
                          'fixture evidence',
                          NULL, NULL, NULL, NULL, 0.93,
                          'llm_reviewed', ?, ?)
                """,
                (rejected_id, timestamp, timestamp),
            )
            conn.commit()

            result = promote_ksa_definitions(conn)
            locked_defined_row = conn.execute(
                """
                SELECT definition, definition_source, definition_status, review_status
                FROM ontology_concepts
                WHERE concept_id = ?
                """,
                (locked_defined_id,),
            ).fetchone()
            locked_reviewed_row = conn.execute(
                """
                SELECT definition, definition_source, definition_status, review_status
                FROM ontology_concepts
                WHERE concept_id = ?
                """,
                (locked_reviewed_id,),
            ).fetchone()
            rejected_row = conn.execute(
                """
                SELECT definition, definition_source, definition_status, review_status
                FROM ontology_concepts
                WHERE concept_id = ?
                """,
                (rejected_id,),
            ).fetchone()
            conn.close()

            self.assertEqual(result["promoted"], 0)
            self.assertEqual(result["skipped_boilerplate"], 0)
            self.assertEqual(result["skipped_human_lock"], 3)
            self.assertEqual(locked_defined_row["definition"], "Human-authored locked definition.")
            self.assertEqual(locked_defined_row["definition_source"], "manual")
            self.assertEqual(locked_defined_row["definition_status"], "defined")
            self.assertEqual(locked_defined_row["review_status"], "raw")
            self.assertIsNone(locked_reviewed_row["definition"])
            self.assertIsNone(locked_reviewed_row["definition_source"])
            self.assertEqual(locked_reviewed_row["definition_status"], "missing")
            self.assertEqual(locked_reviewed_row["review_status"], "human_reviewed")
            self.assertIsNone(rejected_row["definition"])
            self.assertIsNone(rejected_row["definition_source"])
            self.assertEqual(rejected_row["definition_status"], "missing")
            self.assertEqual(rejected_row["review_status"], "rejected")

    def test_retract_boilerplate_definitions_dry_run_and_apply(self) -> None:
        conn = self._memory_conn()
        timestamp = now_utc()
        boilerplate_text = _term_definition_text_for_concept(
            {"concept_name": "Workforce analysis", "concept_type": "knowledge"}
        )
        real_text = "Workforce planning skill: 인력 수요를 분석하고 실행 계획을 수립하는 기술."
        conn.execute(
            """
            INSERT INTO ontology_concepts(
                concept_name, normalized_key, concept_type,
                definition, definition_source, definition_status,
                relation_status, review_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'unlinked', ?, ?, ?)
            """,
            (
                "Workforce analysis",
                "workforceanalysis",
                "knowledge",
                boilerplate_text,
                "ksa_meaning_candidates.term_definition_template",
                "candidate",
                "model_preprocessed",
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO ontology_concepts(
                concept_name, normalized_key, concept_type,
                definition, definition_source, definition_status,
                relation_status, review_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'unlinked', ?, ?, ?)
            """,
            (
                "Workforce planning skill",
                "workforceplanningskill",
                "skill",
                real_text,
                "manual",
                "candidate",
                "raw",
                timestamp,
                timestamp,
            ),
        )
        conn.commit()

        dry_run = retract_boilerplate_definitions(conn, dry_run=True, batch_size=10)
        self.assertEqual(dry_run["retract_eligible"], 1)
        self.assertEqual(dry_run["retracted"], 0)

        applied = retract_boilerplate_definitions(conn, dry_run=False, batch_size=10)
        self.assertEqual(applied["retracted"], 1)
        boilerplate_row = conn.execute(
            """
            SELECT definition, definition_source, definition_status, review_status
            FROM ontology_concepts
            WHERE normalized_key = 'workforceanalysis'
            """
        ).fetchone()
        real_row = conn.execute(
            """
            SELECT definition, definition_source, definition_status, review_status
            FROM ontology_concepts
            WHERE normalized_key = 'workforceplanningskill'
            """
        ).fetchone()
        self.assertIsNone(boilerplate_row["definition"])
        self.assertIsNone(boilerplate_row["definition_source"])
        self.assertEqual(boilerplate_row["definition_status"], "missing")
        self.assertEqual(boilerplate_row["review_status"], "model_preprocessed")
        self.assertEqual(real_row["definition"], real_text)
