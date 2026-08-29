from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ncs_mcp.compact_postings import encode_posting_ids
from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp import server


class CompactOntologyRuntimeTests(unittest.TestCase):
    def test_analysis_and_evidence_read_compact_schema_with_labels(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "compact.db"
            conn = connect(db_path)
            initialize_database(conn)
            timestamp = now_utc()
            conn.executemany(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type, definition,
                    definition_status, relation_status, review_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'missing', 'unlinked', 'candidate', ?, ?)
                """,
                [
                    ("Source concept", "source", "knowledge", "", timestamp, timestamp),
                    ("Target concept", "target", "skill", "", timestamp, timestamp),
                ],
            )
            conn.execute(
                """
                INSERT INTO ontology_concept_aliases(
                    concept_id, alias_text, normalized_alias_key, alias_source, created_at
                ) VALUES (1, 'source alias', 'sourcealias', 'raw', ?)
                """,
                (timestamp,),
            )
            conn.executescript(
                """
                CREATE TABLE ontology_relation_types (
                    relation_type_code INTEGER PRIMARY KEY,
                    relation_type TEXT NOT NULL UNIQUE,
                    relation_label TEXT NOT NULL
                );
                CREATE TABLE ontology_relation_outgoing (
                    source_concept_id INTEGER NOT NULL,
                    relation_type_code INTEGER NOT NULL,
                    target_count INTEGER NOT NULL,
                    target_ids BLOB NOT NULL,
                    PRIMARY KEY(source_concept_id, relation_type_code)
                ) WITHOUT ROWID;
                CREATE TABLE ontology_relation_incoming (
                    target_concept_id INTEGER NOT NULL,
                    relation_type_code INTEGER NOT NULL,
                    source_count INTEGER NOT NULL,
                    source_ids BLOB NOT NULL,
                    PRIMARY KEY(target_concept_id, relation_type_code)
                ) WITHOUT ROWID;
                CREATE TABLE criteria_concept_forward (
                    criteria_id INTEGER PRIMARY KEY,
                    concept_count INTEGER NOT NULL,
                    concept_ids BLOB NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE criteria_concept_inverse (
                    concept_id INTEGER PRIMARY KEY,
                    criteria_count INTEGER NOT NULL,
                    criteria_ids BLOB NOT NULL
                ) WITHOUT ROWID;
                """
            )
            conn.execute(
                "INSERT INTO ontology_relation_types VALUES (1, 'knowledge_enables_skill', 'Knowledge enables skill')"
            )
            conn.execute(
                "INSERT INTO ontology_relation_outgoing VALUES (1, 1, 1, ?)",
                (encode_posting_ids([2]),),
            )
            conn.execute(
                "INSERT INTO ontology_relation_incoming VALUES (2, 1, 1, ?)",
                (encode_posting_ids([1]),),
            )
            conn.execute(
                "INSERT INTO criteria_concept_inverse VALUES (1, 1, ?)",
                (encode_posting_ids([1]),),
            )
            conn.execute("DROP TABLE ontology_concept_relations")
            conn.execute("DROP TABLE criteria_concept_links")
            conn.commit()
            conn.close()

            previous = os.environ.get("NCS_DB_PATH")
            os.environ["NCS_DB_PATH"] = str(db_path)
            try:
                analysis = server.ncs_analysis(mode="ontology", query="Source")
                row = analysis["data"]["concepts"][0]
                self.assertEqual(1, row["relation_count"])
                self.assertEqual(1, row["criteria_link_count"])
                evidence = server.get_concept_evidence(concept_id=1)
            finally:
                if previous is None:
                    os.environ.pop("NCS_DB_PATH", None)
                else:
                    os.environ["NCS_DB_PATH"] = previous

            relation = evidence["data"]["relations"]["outgoing"][0]
            self.assertEqual("knowledge_enables_skill", relation["relation_type"])
            self.assertEqual("Knowledge enables skill", relation["relation_label"])
            self.assertEqual([], evidence["data"]["learning_modules"])
            self.assertEqual([], evidence["data"]["recommendation_evidence"])


if __name__ == "__main__":
    unittest.main()
