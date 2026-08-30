from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.audit_compact_snapshot_size import (
    SCHEMA,
    _cold_inference,
    _readiness_tables,
    render_markdown,
    run_audit,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


class CompactSnapshotSizeAuditTests(unittest.TestCase):
    def make_package(self, root: Path) -> tuple[Path, Path, Path]:
        database = root / "ncs_ontology_compact.db"
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            CREATE TABLE competency_units(unit_code TEXT, unit_name_raw TEXT);
            CREATE TABLE performance_criteria(criteria_id INTEGER, criteria_text_raw TEXT);
            CREATE TABLE ksa_items(ksa_id INTEGER, ksa_text_raw TEXT);
            CREATE TABLE ncs_training_courses(training_course_id INTEGER, course_name TEXT);
            CREATE TABLE ontology_concepts(
                concept_id INTEGER,
                concept_name TEXT,
                normalized_key TEXT,
                concept_type TEXT,
                definition,
                definition_source,
                definition_status TEXT,
                relation_status TEXT,
                review_status TEXT,
                created_at,
                updated_at
            );
            INSERT INTO competency_units VALUES ('U1', 'unit');
            INSERT INTO performance_criteria VALUES (1, 'criterion');
            INSERT INTO ksa_items VALUES (1, 'ksa');
            INSERT INTO ncs_training_courses VALUES (1, 'course');
            INSERT INTO ontology_concepts VALUES
                (1, 'concept', 'concept', 'knowledge', NULL, NULL,
                 'missing', 'candidate', 'auto', NULL, NULL);
            CREATE INDEX idx_units_name_a ON competency_units(unit_name_raw);
            CREATE INDEX idx_units_name_b ON competency_units(unit_name_raw);
            """
        )
        connection.commit()
        connection.close()

        database_sha = sha256(database)
        archive = root / "ncs_ontology_compact.zip"
        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1
        ) as handle:
            handle.write(database, database.name)
        manifest = root / "ncs_ontology_compact.manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "archive_member": database.name,
                    "sqlite_bytes": database.stat().st_size,
                    "sqlite_sha256": database_sha,
                    "physical_counts": {
                        "competency_units": 1,
                        "performance_criteria": 1,
                        "ksa_items": 1,
                        "ncs_training_courses": 1,
                    },
                    "logical_counts": {},
                    "servable_counts": {},
                }
            ),
            encoding="utf-8",
        )
        vercel = root / "vercel.json"
        vercel.write_text(
            json.dumps(
                {
                    "env": {
                        "NCS_MCP_READINESS_EXTRA_TABLES": "ontology_concepts",
                        "NCS_MCP_READINESS_MIN_ROWS": '{"ontology_concepts":1}',
                    }
                }
            ),
            encoding="utf-8",
        )
        return archive, manifest, vercel

    def test_read_only_audit_accounts_for_pages_and_preserves_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, manifest, vercel = self.make_package(root)
            archive_before = sha256(archive)
            manifest_before = sha256(manifest)

            report = run_audit(
                archive,
                manifest,
                vercel_config=vercel,
                root=root,
                trace_public_tools=False,
                run_dictionary_spike=False,
            )

            self.assertEqual(SCHEMA, report["schema"])
            self.assertTrue(report["source_integrity"]["archive_unchanged"])
            self.assertTrue(report["source_integrity"]["manifest_unchanged"])
            self.assertTrue(
                report["source_integrity"]["temporary_directory_cleaned"]
            )
            self.assertEqual(archive_before, sha256(archive))
            self.assertEqual(manifest_before, sha256(manifest))
            self.assertTrue(report["page_accounting"]["dbstat_accounts_for_database"])
            self.assertEqual(0, report["page_accounting"]["freelist_count"])
            self.assertTrue(
                any(
                    item["kind"] == "exact"
                    for item in report["duplicate_or_prefix_index_candidates"]
                )
            )

    def test_readiness_parser_keeps_core_then_deduplicates_extras(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vercel.json"
            path.write_text(
                json.dumps(
                    {
                        "env": {
                            "NCS_MCP_READINESS_EXTRA_TABLES": (
                                "ontology_concepts,competency_units,ontology_concepts"
                            ),
                            "NCS_MCP_READINESS_MIN_ROWS": (
                                '{"ontology_concepts":533909}'
                            ),
                        }
                    }
                ),
                encoding="utf-8",
            )
            contract = _readiness_tables(path)
            self.assertEqual(5, contract["required_table_count"])
            self.assertEqual(533909, contract["minimum_rows"]["ontology_concepts"])

    def test_cold_inference_is_explicitly_bounded_and_marked_inference(self) -> None:
        inference = _cold_inference(25, 100, 4.0, 1.0)
        self.assertEqual(0.25, inference["raw_fraction"])
        self.assertEqual(0.25, inference["local_extract_stage_savings_seconds"])
        self.assertEqual(1.0, inference["full_cold_upper_bound_savings_seconds"])
        self.assertIn("inference", inference["basis"])

    def test_markdown_leads_with_no_change_and_contract_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, manifest, vercel = self.make_package(root)
            report = run_audit(
                archive,
                manifest,
                vercel_config=vercel,
                root=root,
                trace_public_tools=False,
                run_dictionary_spike=False,
            )
            markdown = render_markdown(report)
            self.assertIn("# Compact Snapshot Size Audit", markdown)
            self.assertIn("Readiness required tables: `5`", markdown)
            self.assertIn("Candidate Trims", markdown)
            self.assertIn("linear byte-scaling inferences", markdown)


if __name__ == "__main__":
    unittest.main()
