from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ncs_mcp.embedding_batches import (
    KSA_CONCEPT,
    PERFORMANCE_CRITERION,
    PERFORMANCE_ELEMENT,
    EmbeddingNodeInput,
    _iter_cursor_rows,
    build_embedding_patch_records,
    iter_embedding_input_batches,
)
from ncs_mcp.embeddings import EmbeddingDimensionError


class _RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self._batches = [[1, 2], [3], []]

    def fetchmany(self, size: int):
        self.calls.append(size)
        return self._batches.pop(0)


class EmbeddingBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "embedding.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE competency_elements (
                element_id INTEGER PRIMARY KEY,
                element_name_raw TEXT NOT NULL,
                element_name_refined TEXT
            );
            CREATE TABLE performance_criteria (
                criteria_id INTEGER PRIMARY KEY,
                element_id INTEGER NOT NULL,
                criteria_text_raw TEXT NOT NULL,
                criteria_text_refined TEXT
            );
            CREATE TABLE ontology_concepts (
                concept_id INTEGER PRIMARY KEY,
                concept_name TEXT NOT NULL,
                definition TEXT,
                definition_source TEXT,
                definition_status TEXT,
                review_status TEXT
            );
            INSERT INTO competency_elements VALUES
                (20, '원시 요소', '인력운영계획 수립'),
                (30, '보고서 작성', NULL);
            INSERT INTO performance_criteria VALUES
                (202, 20, '두 번째 기준', NULL),
                (201, 20, '원시 첫 기준', '첫 번째 기준'),
                (301, 30, '결과를 보고할 수 있다.', NULL);
            INSERT INTO ontology_concepts VALUES
                (1, '정원산정', '조직 목표와 업무량을 근거로 적정 인원을 계산하는 방법.',
                 'operator_manual_review', 'defined', 'human_reviewed'),
                (2, '직무분석', '직무분석 : 업무 판단과 문제 해결에 필요한 관련 원리, 기준, 절차, 사례에 대한 지식.',
                 'operator_manual_review', 'defined', 'human_reviewed'),
                (3, '도구활용', '업무 도구를 정확히 사용하는 방법.',
                 'raw_ksa', 'defined', 'human_reviewed'),
                (4, '책임성', '업무 결과에 책임을 지는 자세.',
                 'operator_manual_review', 'defined', 'candidate');
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _records(self, **kwargs) -> list[EmbeddingNodeInput]:
        batches = list(
            iter_embedding_input_batches(
                self.db_path,
                provider_name="provider-a",
                model="model-v1",
                dimensions=3,
                batch_size=2,
                fetch_size=2,
                **kwargs,
            )
        )
        self.assertTrue(all(1 <= len(batch) <= 2 for batch in batches))
        return [record for batch in batches for record in batch]

    def test_batches_are_deterministic_and_elements_include_ordered_criteria(
        self,
    ) -> None:
        first = self._records()
        second = self._records()

        self.assertEqual(first, second)
        self.assertEqual(
            [record.entity_type for record in first],
            [
                PERFORMANCE_CRITERION,
                PERFORMANCE_CRITERION,
                PERFORMANCE_CRITERION,
                PERFORMANCE_ELEMENT,
                PERFORMANCE_ELEMENT,
                KSA_CONCEPT,
                KSA_CONCEPT,
                KSA_CONCEPT,
                KSA_CONCEPT,
            ],
        )

    def test_keyset_ranges_are_disjoint_and_cover_the_same_criterion_ids(self) -> None:
        element = next(
            record
            for record in self._records(entity_types=(PERFORMANCE_ELEMENT,))
            if record.source_key == "20"
        )
        common = {"entity_types": (PERFORMANCE_CRITERION,)}
        all_ids = [record.source_key for record in self._records(**common)]
        first = [
            record.source_key
            for record in self._records(
                source_key_min=201, source_key_max=202, **common
            )
        ]
        second = [
            record.source_key
            for record in self._records(
                source_key_min=301, source_key_max=301, **common
            )
        ]
        self.assertEqual(first + second, all_ids)
        self.assertEqual(len(set(first + second)), len(all_ids))
        self.assertEqual(
            element.text,
            "인력운영계획 수립 첫 번째 기준 두 번째 기준",
        )
        self.assertEqual(element.metadata["criteria_seen"], 2)
        self.assertEqual(element.metadata["criteria_included"], 2)
        self.assertFalse(element.metadata["text_truncated"])

    def test_element_text_is_hard_capped_with_truncation_audit(self) -> None:
        records = self._records(
            entity_types=(PERFORMANCE_ELEMENT,),
            max_text_chars=18,
        )
        self.assertEqual(len(records), 2)
        first = records[0]
        self.assertLessEqual(len(first.text), 18)
        self.assertTrue(first.metadata["text_truncated"])
        self.assertEqual(first.metadata["criteria_seen"], 2)
        self.assertGreater(first.metadata["source_char_count"], len(first.text))

    def test_only_trusted_non_boilerplate_non_raw_definition_is_included(self) -> None:
        concepts = {
            record.source_key: record
            for record in self._records(entity_types=(KSA_CONCEPT,))
        }
        self.assertIn("조직 목표와 업무량", concepts["1"].text)
        self.assertTrue(concepts["1"].metadata["definition_included"])

        self.assertEqual(concepts["2"].text, "직무분석")
        self.assertFalse(concepts["2"].metadata["definition_included"])
        self.assertEqual(concepts["3"].text, "도구활용")
        self.assertFalse(concepts["3"].metadata["definition_source_eligible"])
        self.assertEqual(concepts["4"].text, "책임성")
        self.assertFalse(concepts["4"].metadata["definition_included"])

    def test_fetchmany_is_used_with_the_declared_bound(self) -> None:
        cursor = _RecordingCursor()
        self.assertEqual(list(_iter_cursor_rows(cursor, 2)), [1, 2, 3])
        self.assertEqual(cursor.calls, [2, 2, 2])

    def test_vector_response_is_validated_and_patch_has_no_text(self) -> None:
        inputs = self._records(entity_types=(PERFORMANCE_CRITERION,))[:2]
        patches = build_embedding_patch_records(
            inputs,
            ([0.1, 0.2, 0.3], [1, 2, 3]),
        )
        self.assertEqual(len(patches), 2)
        payload = patches[0].as_dict()
        self.assertEqual(payload["entity_id"], inputs[0].entity_id)
        self.assertEqual(payload["dimensions"], 3)
        self.assertNotIn("text", payload)
        self.assertFalse(payload["metadata"]["db_writes"])
        self.assertFalse(payload["metadata"]["neo4j_writes"])

        with self.assertRaises(EmbeddingDimensionError):
            build_embedding_patch_records(inputs, ([0.1, 0.2, 0.3],))
        with self.assertRaises(EmbeddingDimensionError):
            build_embedding_patch_records(inputs, ([0.1, 0.2], [1, 2]))

    def test_source_database_bytes_are_unchanged(self) -> None:
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self._records()
        after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertFalse(Path(f"{self.db_path}-wal").exists())
        self.assertFalse(Path(f"{self.db_path}-journal").exists())

    def test_missing_optional_concept_table_is_graceful(self) -> None:
        no_concepts = Path(self.tempdir.name) / "no-concepts.db"
        conn = sqlite3.connect(no_concepts)
        conn.executescript(
            """
            CREATE TABLE competency_elements (
                element_id INTEGER PRIMARY KEY,
                element_name_raw TEXT NOT NULL
            );
            CREATE TABLE performance_criteria (
                criteria_id INTEGER PRIMARY KEY,
                element_id INTEGER NOT NULL,
                criteria_text_raw TEXT NOT NULL
            );
            INSERT INTO competency_elements VALUES (1, '요소');
            INSERT INTO performance_criteria VALUES (1, 1, '수행할 수 있다.');
            """
        )
        conn.commit()
        conn.close()

        records = [
            record
            for batch in iter_embedding_input_batches(
                no_concepts,
                provider_name="local",
                model="test",
                dimensions=2,
                batch_size=4,
                fetch_size=1,
            )
            for record in batch
        ]
        self.assertEqual(
            [record.entity_type for record in records],
            [PERFORMANCE_CRITERION, PERFORMANCE_ELEMENT],
        )

    def test_configuration_bounds_are_rejected(self) -> None:
        common = {
            "provider_name": "p",
            "model": "m",
            "dimensions": 3,
        }
        with self.assertRaises(ValueError):
            list(iter_embedding_input_batches(self.db_path, batch_size=257, **common))
        with self.assertRaises(ValueError):
            list(iter_embedding_input_batches(self.db_path, fetch_size=513, **common))
        with self.assertRaises(ValueError):
            list(
                iter_embedding_input_batches(
                    self.db_path,
                    max_text_chars=32_769,
                    **common,
                )
            )
        with self.assertRaises(ValueError):
            list(
                iter_embedding_input_batches(
                    self.db_path,
                    dimensions=8_193,
                    provider_name="p",
                    model="m",
                )
            )


if __name__ == "__main__":
    unittest.main()
