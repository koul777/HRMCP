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
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ksa_meaning_codex_judge import run_meaning_judge

from ncs_mcp.db import connect, initialize_database, now_utc


FORBIDDEN_HUMAN_STATUSES = {"human_reviewed", "accepted", "reviewed"}


class KsaMeaningCodexJudgeTest(unittest.TestCase):
    def _create_fixture(self, db_path: Path) -> dict[str, int]:
        conn = connect(db_path)
        try:
            initialize_database(conn)
            ts = now_utc()
            cur = conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
                """
            )
            classification_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, created_at, updated_at
                ) VALUES ('0202020101_26v1', '0202020101', '26v1',
                          'HR planning', '4', ?, ?, ?)
                """,
                (classification_id, ts, ts),
            )
            ids: dict[str, int] = {}
            for name, key, ctype in (
                ("workforce planning", "workforceplanning", "knowledge"),
                ("unscoped fallback", "unscopedfallback", "skill"),
            ):
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, relation_status, review_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'candidate', 'unlinked', 'model_preprocessed', ?, ?)
                    """,
                    (name, key, ctype, ts, ts),
                )
                ids[f"{key}_concept_id"] = int(cur.lastrowid)
            cur = conn.execute(
                """
                INSERT INTO ksa_meaning_candidates(
                    concept_id, concept_type, meaning_role, meaning_text,
                    source_method, evidence_text, unit_code, element_id,
                    criteria_id, ksa_id, confidence_score, review_status,
                    created_at, updated_at
                ) VALUES (?, 'knowledge', 'term_definition_candidate',
                          'workforce planning: source backed definition candidate',
                          'term_definition_template',
                          'unit: HR planning | criteria: build workforce plan',
                          '0202020101_26v1', NULL, NULL, NULL, 0.72,
                          'candidate', ?, ?)
                """,
                (ids["workforceplanning_concept_id"], ts, ts),
            )
            ids["safe_meaning_id"] = int(cur.lastrowid)
            cur = conn.execute(
                """
                INSERT INTO ksa_meaning_candidates(
                    concept_id, concept_type, meaning_role, meaning_text,
                    source_method, evidence_text, unit_code, element_id,
                    criteria_id, ksa_id, confidence_score, review_status,
                    created_at, updated_at
                ) VALUES (?, 'skill', 'task_skill_significance',
                          'unscoped fallback: fallback candidate without task context',
                          'unlinked_concept_fallback',
                          'concept only fallback evidence',
                          NULL, NULL, NULL, NULL, 0.45,
                          'candidate', ?, ?)
                """,
                (ids["unscopedfallback_concept_id"], ts, ts),
            )
            ids["skipped_meaning_id"] = int(cur.lastrowid)
            conn.commit()
            return ids
        finally:
            conn.close()

    def _run_judge(self, tmp: str, db_path: Path, *, dry_run: bool = False) -> dict[str, object]:
        return run_meaning_judge(
            db_path=db_path,
            out=Path(tmp) / "meaning_judge.json",
            manual_seedpack_out=Path(tmp) / "meaning_manual.jsonl",
            min_confidence=0.70,
            min_text_length=10,
            manual_limit=20,
            dry_run=dry_run,
            mark_skipped_needs_review=True,
        )

    def _meaning_statuses(self, db_path: Path) -> dict[int, str]:
        conn = connect(db_path)
        try:
            return {
                int(row["meaning_id"]): str(row["review_status"])
                for row in conn.execute(
                    "SELECT meaning_id, review_status FROM ksa_meaning_candidates"
                ).fetchall()
            }
        finally:
            conn.close()

    def test_mark_skipped_needs_review_updates_safe_and_skipped_meaning_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)

            report = self._run_judge(tmp, db_path)
            statuses = self._meaning_statuses(db_path)
            manual_rows = [
                json.loads(line)
                for line in (Path(tmp) / "meaning_manual.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(statuses[ids["safe_meaning_id"]], "llm_reviewed")
        self.assertEqual(statuses[ids["skipped_meaning_id"]], "needs_review")
        self.assertEqual(report["candidate_rows_scanned"], 2)
        self.assertEqual(report["auto_approved"], 1)
        self.assertEqual(report["needs_review"], 1)
        self.assertEqual(report["status_writes"], {"llm_reviewed": 1, "needs_review": 1})
        self.assertEqual(report["forbidden_statuses_written"], [])
        self.assertEqual([row["meaning_id"] for row in manual_rows], [ids["skipped_meaning_id"]])
        self.assertEqual(manual_rows[0]["review_status"], "needs_review")
        self.assertIn("unsupported_source_method", report["skipped_by_reason"])
        self.assertIn("missing_unit_context", report["skipped_by_reason"])

    def test_dry_run_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)

            report = self._run_judge(tmp, db_path, dry_run=True)
            statuses = self._meaning_statuses(db_path)

        self.assertEqual(statuses[ids["safe_meaning_id"]], "candidate")
        self.assertEqual(statuses[ids["skipped_meaning_id"]], "candidate")
        self.assertEqual(report["status_writes"], {"llm_reviewed": 0, "needs_review": 0})
        self.assertEqual(report["needs_review_written"], 0)

    def test_run_does_not_write_human_or_concept_definition_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            self._create_fixture(db_path)

            report = self._run_judge(tmp, db_path)
            conn = connect(db_path)
            try:
                meaning_statuses = {
                    str(row["review_status"])
                    for row in conn.execute("SELECT review_status FROM ksa_meaning_candidates").fetchall()
                }
                concept_rows = conn.execute(
                    """
                    SELECT definition_status, review_status
                    FROM ontology_concepts
                    ORDER BY concept_id
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(report["forbidden_statuses_written"], [])
        self.assertFalse(meaning_statuses & FORBIDDEN_HUMAN_STATUSES)
        self.assertEqual(
            [(row["definition_status"], row["review_status"]) for row in concept_rows],
            [("candidate", "model_preprocessed"), ("candidate", "model_preprocessed")],
        )


if __name__ == "__main__":
    unittest.main()
