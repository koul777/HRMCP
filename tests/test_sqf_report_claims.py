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

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.sqf_report_claims import (
    build_sqf_report_claim_decision_sheet,
    build_sqf_corpus_audit,
    build_sqf_report_claim_candidates,
    write_sqf_report_claim_decision_sheet_csv,
    write_sqf_report_claim_decision_sheet_html,
    write_sqf_corpus_audit_markdown,
    write_sqf_report_claim_candidates_jsonl,
    write_sqf_report_claim_candidates_markdown,
)
from ncs_mcp.sqf_guarded_import_plan import build_sqf_guarded_import_plan
from ncs_mcp.sqf_human_review_readiness import build_sqf_human_review_readiness
from ncs_mcp.sqf_review_decision_audit import build_sqf_review_decision_audit
from ncs_mcp.sqf_review_priority import prioritize_sqf_claim_candidates


class SqfReportClaimTests(unittest.TestCase):
    def _build_sample_db(self, db_path: Path, raw_file: Path) -> None:
        raw_file.write_text("sample pdf placeholder", encoding="utf-8")
        conn = connect(db_path)
        initialize_database(conn)
        timestamp = now_utc()
        conn.execute(
            """
            INSERT INTO classifications(
                major_code, major_name, middle_code, middle_name,
                small_code, small_name, sub_code, sub_name
            ) VALUES ('02', 'Business', '03', 'Finance Accounting', '02', 'Accounting', '01', 'Accounting Audit')
            """
        )
        classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
            "classification_id"
        ]
        conn.execute(
            """
            INSERT INTO competency_units(
                unit_code, base_unit_code, unit_version, unit_name_raw,
                unit_level_raw, classification_id, created_at, updated_at
            ) VALUES ('0203020101_20v4', '0203020101', '20v4',
                'Accounting Statement Management', '3', ?, ?, ?)
            """,
            (classification_id, timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO sqf_duties(
                source_key, ncs_lclas_cd, ncs_lclas_name, sqf_field_name,
                sqf_sub_field_name, job_name, duty_name, duty_level,
                duty_definition, source_payload, api_fetched_at
            ) VALUES (
                'sqf:test:accounting:3', '02', 'Business', 'Business Support',
                'Accounting', 'Accounting', 'Accounting statement duty', '3',
                'Process accounting statements and accounting information.', '{}', ?
            )
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_industry_sectors(
                sector_id, ncs_lclas_cd, ncs_lclas_name, sqf_field_name,
                sqf_sub_field_name, sector_name, source_count, updated_at
            ) VALUES (
                'sector:02:accounting', '02', 'Business', 'Business Support',
                'Accounting', 'Business Support Accounting', 1, ?
            )
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_jobs_normalized(
                sqf_job_id, sector_id, job_name, job_definition,
                source_count, updated_at
            ) VALUES (
                'job:accounting', 'sector:02:accounting', 'Accounting',
                'Accounting job profile.', 1, ?
            )
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_levels(sqf_level, level_name, definition, updated_at)
            VALUES (3, 'Practitioner', 'Practitioner level.', ?)
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_job_levels_normalized(
                sqf_job_level_id, sqf_job_id, sqf_source_key, duty_name,
                sqf_level, level_name, job_level_definition, duty_definition,
                updated_at
            ) VALUES (
                'job-level:accounting:3', 'job:accounting', 'sqf:test:accounting:3',
                'Accounting statement duty', 3, 'Practitioner',
                'Perform accounting statement processing and produce accounting information.',
                'Process accounting statements and accounting information.', ?
            )
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_ncs_matches(
                source_type, source_id, target_type, target_id, relation,
                score, confidence, match_method, evidence_text, evidence_source,
                review_status, created_at, updated_at
            ) VALUES (
                'sqf_duty', 'sqf:test:accounting:3', 'ncs_competency_unit',
                '0203020101_20v4', 'closeMatch', 12.5, 'lexical',
                'unit_name_overlap', 'Accounting duty overlaps Accounting Statement Management.',
                'test', 'candidate', ?, ?
            )
            """,
            (timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO sqf_library_posts(
                lib_seq, title, source_url, collected_at, ontology_role
            ) VALUES ('lib:1', '2022 SQF Accounting Report', 'local:test', ?, 'reference')
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_library_files(
                lib_seq, sys_dstin_cd, file_mstky, file_detl_seq,
                local_path, download_status
            ) VALUES ('lib:1', 'NCS', 'file:1', '1', ?, 'downloaded')
            """,
            (str(raw_file),),
        )
        file_id = conn.execute("SELECT file_id FROM sqf_library_files").fetchone()["file_id"]
        conn.execute(
            """
            INSERT INTO sqf_document_sources(
                lib_seq, file_id, title, ontology_role, local_path,
                text_extraction_status, created_at
            ) VALUES ('lib:1', ?, '2022 SQF Accounting Report', 'reference', ?, 'extracted', ?)
            """,
            (file_id, str(raw_file), timestamp),
        )
        document_id = conn.execute("SELECT document_id FROM sqf_document_sources").fetchone()[
            "document_id"
        ]
        conn.execute(
            """
            INSERT INTO sqf_document_assets(
                document_id, asset_path, asset_name, asset_type,
                extraction_status, created_at
            ) VALUES (?, ?, 'sample.pdf', 'pdf', 'extracted', ?)
            """,
            (document_id, str(raw_file), timestamp),
        )
        asset_id = conn.execute("SELECT asset_id FROM sqf_document_assets").fetchone()["asset_id"]
        conn.execute(
            """
            INSERT INTO sqf_document_pages(
                asset_id, page_no, text, char_count, extraction_status, created_at
            ) VALUES (?, 12, 'Accounting statement duty evidence text.', 40, 'extracted', ?)
            """,
            (asset_id, timestamp),
        )
        conn.execute(
            """
            INSERT INTO sqf_document_chunks(
                asset_id, chunk_index, page_start, page_end, text,
                char_count, token_estimate, created_at
            ) VALUES (?, 0, 12, 12, ?, 120, 40, ?)
            """,
            (
                asset_id,
                "Accounting statement duty performs accounting statement processing and reporting.",
                timestamp,
            ),
        )
        chunk_id = conn.execute("SELECT chunk_id FROM sqf_document_chunks").fetchone()["chunk_id"]
        conn.execute(
            """
            INSERT INTO sqf_chunk_job_level_matches(
                chunk_id, sqf_job_level_id, sqf_source_key, relation,
                score, method, evidence_text, matched_terms_json,
                review_status, created_at
            ) VALUES (?, 'job-level:accounting:3', 'sqf:test:accounting:3',
                'strongEvidence', 18.5, 'pdf_chunk_lexical_precision_v1',
                'Accounting statement duty performs accounting statement processing.',
                '{"exact": ["Accounting", "Accounting statement duty"]}', 'candidate', ?)
            """,
            (chunk_id, timestamp),
        )
        conn.commit()
        conn.close()

    def test_build_sqf_corpus_audit_is_report_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            raw_file = Path(tmp) / "sample.pdf"
            self._build_sample_db(db_path, raw_file)

            report = build_sqf_corpus_audit(db_path)

            self.assertTrue(report["ok"])
            self.assertEqual(report["format_version"], "ncs-sqf-corpus-audit-v1")
            self.assertEqual(report["summary"]["official_file_count"], 1)
            self.assertEqual(report["summary"]["missing_official_downloaded_files"], 0)
            self.assertEqual(report["summary"]["chunk_count"], 1)
            self.assertEqual(report["summary"]["chunk_match_count"], 1)
            self.assertFalse(report["used_for_scoring"])
            self.assertFalse(report["status_update_allowed"])
            self.assertFalse(report["approval_ready"])

    def test_build_sqf_report_claim_candidates_requires_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            raw_file = Path(tmp) / "sample.pdf"
            out_jsonl = Path(tmp) / "claims.jsonl"
            out_md = Path(tmp) / "claims.md"
            audit_md = Path(tmp) / "audit.md"
            self._build_sample_db(db_path, raw_file)

            audit = build_sqf_corpus_audit(db_path)
            write_sqf_corpus_audit_markdown(audit, audit_md)
            report = build_sqf_report_claim_candidates(
                db_path,
                major_code="02",
                keywords=["Accounting"],
                limit=5,
            )
            write_sqf_report_claim_candidates_jsonl(report, out_jsonl)
            write_sqf_report_claim_candidates_markdown(report, out_md)

            self.assertTrue(report["ok"])
            self.assertEqual(report["batch"]["claim_count"], 1)
            self.assertFalse(report["batch"]["used_for_scoring"])
            self.assertFalse(report["batch"]["status_update_allowed"])
            claim = report["claims"][0]
            self.assertEqual(claim["claim_type"], "sqf_ncs_alignment")
            self.assertEqual(claim["claim_status"], "candidate_requires_human_review")
            self.assertEqual(claim["decision"], "")
            self.assertFalse(claim["used_for_scoring"])
            self.assertFalse(claim["approval_claim"])
            self.assertEqual(claim["import_policy"], "guarded_human_import_only")
            self.assertEqual(claim["basis_strength"]["mapping_relation"], "closeMatch")
            self.assertEqual(claim["basis_strength"]["report_evidence_count"], 1)
            self.assertEqual(claim["recommended_priority"], "P0")
            self.assertEqual(claim["level_gap"], 0)
            self.assertEqual(claim["level_status"], "aligned")
            self.assertFalse(claim["generic_duty_flag"])
            self.assertFalse(claim["cross_scope_name_only_risk"])
            self.assertEqual(claim["evidence_strength"], "strong")
            self.assertEqual(claim["scope_alignment"], "exact_scope")
            action_bundle = claim["review_action_bundle"]
            self.assertEqual(action_bundle["claim_id"], claim["claim_id"])
            self.assertEqual(action_bundle["claim_type"], "sqf_ncs_alignment")
            self.assertEqual(action_bundle["ncs_scope"]["unit_code"], "0203020101_20v4")
            self.assertEqual(action_bundle["evidence_strength"], "strong")
            self.assertEqual(action_bundle["review_risk_flags"], [])
            self.assertIn("approve_for_reference", action_bundle["decision_facets"])
            self.assertFalse(action_bundle["blocking_rules"]["status_update_allowed"])
            self.assertFalse(action_bundle["blocking_rules"]["mutates_scoring"])
            self.assertFalse(action_bundle["blocking_rules"]["saves_review_state"])
            serialized = json.dumps(report, ensure_ascii=False)
            for forbidden in ["asset_path", "local_path", "db_path", "source_payload", "raw_payload", "raw_response"]:
                self.assertNotIn(forbidden, serialized)
            records = [json.loads(line) for line in out_jsonl.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records[0]["record_type"], "batch")
            self.assertEqual(records[1]["record_type"], "sqf_report_claim_candidate")
            self.assertIn("SQF Report Claim Candidates", out_md.read_text(encoding="utf-8"))
            self.assertIn("review_risk_flags", out_md.read_text(encoding="utf-8"))
            self.assertIn("SQF Corpus Audit", audit_md.read_text(encoding="utf-8"))

    def test_build_sqf_report_claim_decision_sheet_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            raw_file = Path(tmp) / "sample.pdf"
            claim_json = Path(tmp) / "claims.json"
            summary_json = Path(tmp) / "decision-summary.json"
            csv_out = Path(tmp) / "decision.csv"
            html_out = Path(tmp) / "decision.html"
            self._build_sample_db(db_path, raw_file)
            report = build_sqf_report_claim_candidates(
                db_path,
                major_code="02",
                keywords=["Accounting"],
                limit=5,
            )
            claim_json.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

            sheet = build_sqf_report_claim_decision_sheet(
                claim_json,
                source_packet="reports/claims.json",
            )
            sheet["rows"][0]["job_name"] = "=SQF job"
            sheet["rows"][0]["duty_name"] = " @SQF duty"
            write_sqf_report_claim_decision_sheet_csv(sheet, csv_out)
            write_sqf_report_claim_decision_sheet_html(sheet, html_out)
            summary_json.write_text(json.dumps({k: v for k, v in sheet.items() if k != "rows"}), encoding="utf-8")

            self.assertTrue(sheet["ok"])
            self.assertFalse(sheet["status_update_allowed"])
            self.assertFalse(sheet["used_for_scoring"])
            self.assertFalse(sheet["approval_claim"])
            self.assertFalse(sheet["db_writes"])
            self.assertEqual(sheet["row_count"], 1)
            row = sheet["rows"][0]
            self.assertEqual(row["decision"], "")
            self.assertEqual(row["status_update_allowed"], "false")
            self.assertEqual(row["used_for_scoring"], "false")
            self.assertEqual(row["approval_claim"], "false")
            self.assertEqual(row["recommended_priority"], "P0")
            self.assertEqual(row["level_status"], "aligned")
            self.assertEqual(row["generic_duty_flag"], "False")
            self.assertEqual(row["cross_scope_name_only_risk"], "False")
            self.assertEqual(row["review_risk_flags"], "")
            self.assertIn("work-scope fit", row["review_action_hint"])
            self.assertIn("status_update_allowed", row["blocking_rules"])
            self.assertIn("SQF Claim Human Review Decision Sheet", html_out.read_text(encoding="utf-8"))
            csv_text = csv_out.read_text(encoding="utf-8-sig")
            self.assertIn("claim_id", csv_text)
            self.assertIn("review_action_hint", csv_text)
            self.assertIn("status_update_allowed", csv_text)
            self.assertIn("'=SQF job", csv_text)
            self.assertIn("' @SQF duty", csv_text)
            self.assertTrue(summary_json.exists())
            serialized_sheet = json.dumps(sheet, ensure_ascii=False)
            for forbidden in ["asset_path", "local_path", "db_path", "source_payload", "raw_payload", "raw_response"]:
                self.assertNotIn(forbidden, serialized_sheet)

    def test_decision_sheet_blocks_absolute_source_packet_without_exporting_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            raw_file = Path(tmp) / "sample.pdf"
            claim_json = Path(tmp) / "claims.json"
            self._build_sample_db(db_path, raw_file)
            report = build_sqf_report_claim_candidates(
                db_path,
                major_code="02",
                keywords=["Accounting"],
                limit=5,
            )
            claim_json.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

            sheet = build_sqf_report_claim_decision_sheet(
                claim_json,
                source_packet=str(claim_json.resolve()),
            )

            self.assertFalse(sheet["ok"])
            self.assertEqual(sheet["source_packet"], claim_json.name)
            self.assertEqual(sheet["rows"][0]["source_packet"], claim_json.name)
            self.assertEqual(sheet["findings"][0]["code"], "source_packet_not_portable")
            serialized_sheet = json.dumps(sheet, ensure_ascii=False)
            self.assertNotIn(str(claim_json.resolve()), serialized_sheet)

    def test_sqf_corpus_audit_does_not_export_internal_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            raw_file = Path(tmp) / "sample.pdf"
            self._build_sample_db(db_path, raw_file)
            conn = connect(db_path)
            conn.execute(
                """
                UPDATE sqf_library_files
                SET error_message = ?
                """,
                (f"failed from {raw_file.resolve()} with source_payload marker",),
            )
            conn.commit()
            conn.close()
            raw_file.unlink()

            report = build_sqf_corpus_audit(db_path)

            serialized = json.dumps(report, ensure_ascii=False)
            for forbidden in ["asset_path", "local_path", "db_path", "source_payload", "raw_payload", "raw_response"]:
                self.assertNotIn(forbidden, serialized)
            self.assertNotIn(str(raw_file.resolve()), serialized)
            missing = report["file_audit"]["missing_official_downloaded_files"][0]
            self.assertTrue(missing["download_error_present"])
            self.assertEqual(report["summary"]["missing_official_downloaded_files"], 1)

    def test_sqf_review_export_chain_does_not_mutate_db_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "ncs.db"
            raw_file = tmp_path / "sample.pdf"
            claim_json = tmp_path / "claims.json"
            priority_json = tmp_path / "priority.json"
            decision_json = tmp_path / "decision.json"
            decision_csv = tmp_path / "decision.csv"
            self._build_sample_db(db_path, raw_file)
            before_stat = db_path.stat()
            wal_path = Path(str(db_path) + "-wal")
            shm_path = Path(str(db_path) + "-shm")
            self.assertFalse(wal_path.exists())
            self.assertFalse(shm_path.exists())

            corpus = build_sqf_corpus_audit(db_path)
            claims = build_sqf_report_claim_candidates(
                db_path,
                major_code="02",
                keywords=["Accounting"],
                limit=5,
            )
            claim_json.write_text(json.dumps(claims, ensure_ascii=False), encoding="utf-8")
            sheet = build_sqf_report_claim_decision_sheet(
                claim_json,
                source_packet="claims.json",
            )
            write_sqf_report_claim_decision_sheet_csv(sheet, decision_csv)
            decision_audit = build_sqf_review_decision_audit(decision_csv)
            decision_json.write_text(json.dumps(decision_audit, ensure_ascii=False), encoding="utf-8")
            priority = prioritize_sqf_claim_candidates(claims, source_path=claim_json)
            priority_json.write_text(json.dumps(priority, ensure_ascii=False), encoding="utf-8")
            readiness = build_sqf_human_review_readiness(
                corpus_audit_path=None,
                claim_report_path=claim_json,
                priority_report_path=priority_json,
                decision_audit_path=decision_json,
                additional_artifact_paths=[decision_csv],
            )
            guarded_plan = build_sqf_guarded_import_plan(
                decision_sheet_path=decision_csv,
                claim_report_path=claim_json,
                decision_audit_path=decision_json,
                db_path=db_path,
                run_artifact_name="guarded-plan.json",
            )
            after_stat = db_path.stat()

            self.assertTrue(corpus["ok"])
            self.assertTrue(claims["ok"])
            self.assertTrue(sheet["ok"])
            self.assertTrue(decision_audit["ok"])
            self.assertTrue(priority["ok"])
            self.assertTrue(readiness["ok"])
            self.assertTrue(guarded_plan["ok"])
            self.assertEqual(after_stat.st_mtime_ns, before_stat.st_mtime_ns)
            self.assertEqual(after_stat.st_size, before_stat.st_size)
            self.assertFalse(wal_path.exists())
            self.assertFalse(shm_path.exists())


if __name__ == "__main__":
    unittest.main()
