from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.collect_api import upsert_sqf_items
from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.ontology import build_sqf_mapping_candidates
from ncs_mcp.ontology_export import export_ontology_jsonld, validate_ontology_readiness
from ncs_mcp.sqf_sqlite import build_sqf_sqlite_model


class OntologyExportTests(unittest.TestCase):
    def test_validate_and_export_jsonld(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            out_path = Path(tmp) / "ontology.jsonld"
            conn = connect(db_path)
            initialize_database(conn)
            ts = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
                """
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_definition,
                    api_match_status, created_at, updated_at
                ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                          '6', ?, 'Plan HR strategy.', 'matched', ?, ?)
                """,
                (classification_id, ts, ts),
            )
            upsert_sqf_items(
                conn,
                [
                    {
                        "ncsLclasCd": "02",
                        "ncsLclasCdnm": "Business",
                        "sqfFldCdnm": "Management",
                        "jobCdnm": "HR",
                        "dutyNm": "HR(6)",
                        "dutyLevel": "6",
                        "dutyDef": "Plan HR strategy.",
                    }
                ],
            )
            conn.commit()
            build_sqf_sqlite_model(db_path)
            build_sqf_mapping_candidates(conn, mvp_only=False, major_code="02")
            conn.close()

            validation = validate_ontology_readiness(db_path)
            self.assertIn("counts", validation)
            self.assertIn("metrics", validation)

            export = export_ontology_jsonld(db_path, out_path, include_chunk_evidence=False)
            self.assertTrue(out_path.exists())
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["@type"], "schema:Dataset")
            self.assertGreater(export["nodes_and_edges"], 0)


if __name__ == "__main__":
    unittest.main()
