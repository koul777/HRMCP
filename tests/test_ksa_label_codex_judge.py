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

from ksa_label_codex_judge import run_codex_judge

from ncs_mcp.db import connect, initialize_database, now_utc


FORBIDDEN_HUMAN_STATUSES = {"human_reviewed", "accepted", "reviewed"}


class KsaLabelCodexJudgeTest(unittest.TestCase):
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
            cur = conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw,
                    element_name_raw, element_level_raw
                ) VALUES ('0202020101_26v1', '1', '01', 'Workforce plan', '4')
                """
            )
            element_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ksa_items(
                    element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                ) VALUES (?, 'K', 'knowledge', '1', 'workforce planning source')
                """,
                (element_id,),
            )
            safe_ksa_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ksa_items(
                    element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                ) VALUES (?, 'K', 'knowledge', '2', 'structured interview source')
                """,
                (element_id,),
            )
            skipped_ksa_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type,
                    definition_status, relation_status, review_status,
                    created_at, updated_at
                ) VALUES ('workforce planning source', 'workforceplanningsource',
                          'knowledge', 'missing', 'unlinked', 'raw', ?, ?)
                """,
                (ts, ts),
            )
            safe_concept_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type,
                    definition_status, relation_status, review_status,
                    created_at, updated_at
                ) VALUES ('structured interview source', 'structuredinterviewsource',
                          'knowledge', 'missing', 'unlinked', 'raw', ?, ?)
                """,
                (ts, ts),
            )
            skipped_concept_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ontology_concept_label_candidates(
                    concept_id, source_ksa_id, source_scope_key, concept_type,
                    source_text, label_text, normalized_label_key, label_role,
                    source_method, candidate_rank, confidence_score,
                    review_status, created_at, updated_at
                ) VALUES (?, ?, '02:02:02:01', 'knowledge',
                          'workforce planning source', 'workforce planning',
                          'workforceplanning', 'short_representative_label',
                          'rule_based_short_label_candidate', 1, 0.88,
                          'candidate', ?, ?)
                """,
                (safe_concept_id, safe_ksa_id, ts, ts),
            )
            safe_label_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ontology_concept_label_candidates(
                    concept_id, source_ksa_id, source_scope_key, concept_type,
                    source_text, label_text, normalized_label_key, label_role,
                    source_method, candidate_rank, confidence_score,
                    review_status, created_at, updated_at
                ) VALUES (?, ?, '02:02:02:01', 'knowledge',
                          'structured interview source', 'abc',
                          'abc', 'short_representative_label',
                          'rule_based_short_label_candidate', 1, 0.2,
                          'candidate', ?, ?)
                """,
                (skipped_concept_id, skipped_ksa_id, ts, ts),
            )
            skipped_label_id = cur.lastrowid
            conn.commit()
            return {
                "safe_label_id": int(safe_label_id),
                "skipped_label_id": int(skipped_label_id),
                "skipped_ksa_id": int(skipped_ksa_id),
                "safe_concept_id": int(safe_concept_id),
                "skipped_concept_id": int(skipped_concept_id),
            }
        finally:
            conn.close()

    def _run_judge(self, tmp: str, db_path: Path, *, dry_run: bool = False) -> dict[str, object]:
        return run_codex_judge(
            db_path=db_path,
            out=Path(tmp) / "judge.json",
            manual_seedpack_out=Path(tmp) / "manual.jsonl",
            min_confidence=0.75,
            min_ratio=0.35,
            max_ratio=0.98,
            manual_limit=20,
            dry_run=dry_run,
            mark_skipped_needs_review=True,
        )

    def _insert_needs_review_label(
        self,
        db_path: Path,
        *,
        concept_id: int,
        ksa_id: int,
        source_text: str,
        label_text: str,
        source_method: str = "already_short_label",
        source_scope_key: str = "02:02:02:01",
        confidence_score: float = 0.52,
    ) -> int:
        conn = connect(db_path)
        try:
            ts = now_utc()
            cur = conn.execute(
                """
                INSERT INTO ontology_concept_label_candidates(
                    concept_id, source_ksa_id, source_scope_key, concept_type,
                    source_text, label_text, normalized_label_key, label_role,
                    source_method, candidate_rank, confidence_score,
                    review_status, created_at, updated_at
                ) VALUES (?, ?, ?, 'knowledge',
                          ?, ?, ?,
                          'short_representative_label',
                          ?, 1, ?,
                          'needs_review', ?, ?)
                """,
                (
                    concept_id,
                    ksa_id,
                    source_scope_key,
                    source_text,
                    label_text,
                    label_text.replace(" ", "").lower(),
                    source_method,
                    confidence_score,
                    ts,
                    ts,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def _label_statuses(self, db_path: Path) -> dict[int, str]:
        conn = connect(db_path)
        try:
            return {
                int(row["label_id"]): str(row["review_status"])
                for row in conn.execute(
                    """
                    SELECT label_id, review_status
                    FROM ontology_concept_label_candidates
                    """
                ).fetchall()
            }
        finally:
            conn.close()

    def test_mark_skipped_needs_review_updates_safe_and_skipped_low_confidence_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)

            report = self._run_judge(tmp, db_path)
            statuses = self._label_statuses(db_path)
            manual_rows = [
                json.loads(line)
                for line in (Path(tmp) / "manual.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(statuses[ids["safe_label_id"]], "llm_reviewed")
        self.assertEqual(statuses[ids["skipped_label_id"]], "needs_review")
        self.assertEqual(report["candidate_rows_scanned"], 2)
        self.assertEqual(report["auto_approved"], 1)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(report["needs_review"], 1)
        self.assertEqual(report["needs_review_written"], 1)
        self.assertEqual(report["manual_seedpack_rows"], 1)
        self.assertEqual(report["status_writes"], {"llm_reviewed": 1, "needs_review": 1})
        self.assertEqual([row["label_id"] for row in manual_rows], [ids["skipped_label_id"]])
        self.assertEqual(manual_rows[0]["review_status"], "needs_review")
        self.assertEqual(manual_rows[0]["triage_target_review_status"], "needs_review")
        self.assertIn("confidence_too_low", report["skipped_by_reason"])
        self.assertIn("label_too_short", report["skipped_by_reason"])
        self.assertFalse(report["criteria"]["confidence_filter_applied"])

    def test_mark_skipped_needs_review_dry_run_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)

            report = self._run_judge(tmp, db_path, dry_run=True)
            statuses = self._label_statuses(db_path)
            manual_rows = [
                json.loads(line)
                for line in (Path(tmp) / "manual.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(statuses[ids["safe_label_id"]], "candidate")
        self.assertEqual(statuses[ids["skipped_label_id"]], "candidate")
        self.assertEqual(report["auto_approved"], 1)
        self.assertEqual(report["needs_review"], 1)
        self.assertEqual(report["status_writes"], {"llm_reviewed": 0, "needs_review": 0})
        self.assertEqual(report["needs_review_written"], 0)
        self.assertEqual(report["manual_seedpack_rows"], 1)
        self.assertEqual(manual_rows[0]["review_status"], "candidate")
        self.assertEqual(manual_rows[0]["triage_target_review_status"], "needs_review")

    def test_mark_skipped_needs_review_reports_seedpack_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)
            conn = connect(db_path)
            try:
                ts = now_utc()
                conn.execute(
                    """
                    INSERT INTO ontology_concept_label_candidates(
                        concept_id, source_ksa_id, source_scope_key, concept_type,
                        source_text, label_text, normalized_label_key, label_role,
                        source_method, candidate_rank, confidence_score,
                        review_status, created_at, updated_at
                    ) VALUES (?, ?, '02:02:02:01', 'knowledge',
                              'structured interview source', 'def',
                              'def', 'short_representative_label',
                              'rule_based_short_label_candidate', 2, 0.1,
                              'candidate', ?, ?)
                    """,
                    (ids["skipped_concept_id"], ids["skipped_ksa_id"], ts, ts),
                )
                conn.commit()
            finally:
                conn.close()

            report = run_codex_judge(
                db_path=db_path,
                out=Path(tmp) / "judge.json",
                manual_seedpack_out=Path(tmp) / "manual.jsonl",
                min_confidence=0.75,
                min_ratio=0.35,
                max_ratio=0.98,
                manual_limit=1,
                dry_run=False,
                mark_skipped_needs_review=True,
            )
            manual_rows = [
                json.loads(line)
                for line in (Path(tmp) / "manual.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(report["needs_review"], 2)
        self.assertEqual(report["needs_review_written"], 2)
        self.assertEqual(report["manual_seedpack_rows"], 1)
        self.assertEqual(report["manual_seedpack_limit"], 1)
        self.assertEqual(report["manual_seedpack_total_eligible"], 2)
        self.assertTrue(report["manual_seedpack_truncated"])
        self.assertEqual(report["manual_seedpack_omitted_rows"], 1)
        self.assertEqual(report["manual_seedpack_scope"], "needs_review_top_sample")
        self.assertEqual(len(manual_rows), 1)

    def test_promote_safe_already_short_needs_review_to_llm_reviewed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)
            promoted_id = self._insert_needs_review_label(
                db_path,
                concept_id=ids["safe_concept_id"],
                ksa_id=ids["skipped_ksa_id"],
                source_text="workforce planning",
                label_text="workforce planning",
            )

            report = run_codex_judge(
                db_path=db_path,
                out=Path(tmp) / "judge.json",
                manual_seedpack_out=Path(tmp) / "manual.jsonl",
                min_confidence=0.75,
                min_ratio=0.35,
                max_ratio=0.98,
                manual_limit=20,
                dry_run=False,
                mark_skipped_needs_review=False,
                promote_safe_already_short_needs_review=True,
            )
            statuses = self._label_statuses(db_path)

        self.assertEqual(statuses[promoted_id], "llm_reviewed")
        self.assertEqual(report["safe_already_short_rows_scanned"], 1)
        self.assertEqual(report["safe_already_short_promoted"], 1)
        self.assertEqual(report["safe_already_short_promoted_written"], 1)
        self.assertEqual(report["safe_already_short_skipped"], 0)
        self.assertEqual(report["status_writes"]["llm_reviewed"], 2)
        self.assertEqual(report["forbidden_statuses_written"], [])

    def test_does_not_promote_needs_review_when_source_method_is_not_already_short(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)
            blocked_id = self._insert_needs_review_label(
                db_path,
                concept_id=ids["skipped_concept_id"],
                ksa_id=ids["skipped_ksa_id"],
                source_text="benefit design",
                label_text="benefit design",
                source_method="rule_based_short_label_candidate",
            )

            report = run_codex_judge(
                db_path=db_path,
                out=Path(tmp) / "judge.json",
                manual_seedpack_out=Path(tmp) / "manual.jsonl",
                min_confidence=0.75,
                min_ratio=0.35,
                max_ratio=0.98,
                manual_limit=20,
                dry_run=False,
                mark_skipped_needs_review=False,
                promote_safe_already_short_needs_review=True,
            )
            statuses = self._label_statuses(db_path)

        self.assertEqual(statuses[blocked_id], "needs_review")
        self.assertEqual(report["safe_already_short_rows_scanned"], 0)
        self.assertEqual(report["safe_already_short_promoted"], 0)

    def test_promote_safe_rule_based_needs_review_parenthetical_short_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)
            promoted_id = self._insert_needs_review_label(
                db_path,
                concept_id=ids["safe_concept_id"],
                ksa_id=ids["skipped_ksa_id"],
                source_text="역할 및 책임(Role & Responsibility; R&R) 정의 기술",
                label_text="역할 및 책임 정의",
                source_method="rule_based_short_label_candidate",
                confidence_score=0.83,
            )

            report = run_codex_judge(
                db_path=db_path,
                out=Path(tmp) / "judge.json",
                manual_seedpack_out=Path(tmp) / "manual.jsonl",
                min_confidence=0.75,
                min_ratio=0.35,
                max_ratio=0.98,
                manual_limit=20,
                dry_run=False,
                mark_skipped_needs_review=False,
                promote_safe_rule_based_needs_review=True,
            )
            statuses = self._label_statuses(db_path)

        self.assertEqual(statuses[promoted_id], "llm_reviewed")
        self.assertEqual(report["safe_rule_based_rows_scanned"], 1)
        self.assertEqual(report["safe_rule_based_promoted"], 1)
        self.assertEqual(report["safe_rule_based_promoted_written"], 1)
        self.assertEqual(report["safe_rule_based_skipped"], 0)
        self.assertEqual(report["forbidden_statuses_written"], [])

    def test_does_not_promote_safe_rule_based_needs_review_with_dangling_ending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)
            blocked_id = self._insert_needs_review_label(
                db_path,
                concept_id=ids["safe_concept_id"],
                ksa_id=ids["skipped_ksa_id"],
                source_text="일정단축을 위한 기술(Crashing, Fast Tracking)",
                label_text="일정단축을 위한",
                source_method="rule_based_short_label_candidate",
                confidence_score=0.83,
            )

            report = run_codex_judge(
                db_path=db_path,
                out=Path(tmp) / "judge.json",
                manual_seedpack_out=Path(tmp) / "manual.jsonl",
                min_confidence=0.75,
                min_ratio=0.35,
                max_ratio=0.98,
                manual_limit=20,
                dry_run=False,
                mark_skipped_needs_review=False,
                promote_safe_rule_based_needs_review=True,
            )
            statuses = self._label_statuses(db_path)

        self.assertEqual(statuses[blocked_id], "needs_review")
        self.assertEqual(report["safe_rule_based_rows_scanned"], 1)
        self.assertEqual(report["safe_rule_based_promoted"], 0)
        self.assertEqual(report["safe_rule_based_skipped"], 1)
        self.assertIn("dangling_label_ending", report["safe_rule_based_skipped_by_reason"])

    def test_promote_safe_source_faithful_exact_already_short_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)
            promoted_id = self._insert_needs_review_label(
                db_path,
                concept_id=ids["safe_concept_id"],
                ksa_id=ids["skipped_ksa_id"],
                source_text="기획력",
                label_text="기획력",
                source_method="already_short_label",
                confidence_score=0.42,
            )

            report = run_codex_judge(
                db_path=db_path,
                out=Path(tmp) / "judge.json",
                manual_seedpack_out=Path(tmp) / "manual.jsonl",
                min_confidence=0.75,
                min_ratio=0.35,
                max_ratio=0.98,
                manual_limit=20,
                dry_run=False,
                mark_skipped_needs_review=False,
                promote_safe_source_faithful_needs_review=True,
            )
            statuses = self._label_statuses(db_path)

        self.assertEqual(statuses[promoted_id], "llm_reviewed")
        self.assertEqual(report["safe_source_faithful_rows_scanned"], 1)
        self.assertEqual(report["safe_source_faithful_promoted"], 1)
        self.assertEqual(report["safe_source_faithful_promoted_written"], 1)

    def test_promote_safe_source_faithful_suffix_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)
            promoted_id = self._insert_needs_review_label(
                db_path,
                concept_id=ids["safe_concept_id"],
                ksa_id=ids["skipped_ksa_id"],
                source_text="GIS(Geographic Information System) 운용 기술",
                label_text="GIS 운용",
                source_method="rule_based_short_label_candidate",
                confidence_score=0.73,
            )

            report = run_codex_judge(
                db_path=db_path,
                out=Path(tmp) / "judge.json",
                manual_seedpack_out=Path(tmp) / "manual.jsonl",
                min_confidence=0.75,
                min_ratio=0.35,
                max_ratio=0.98,
                manual_limit=20,
                dry_run=False,
                mark_skipped_needs_review=False,
                promote_safe_source_faithful_needs_review=True,
            )
            statuses = self._label_statuses(db_path)

        self.assertEqual(statuses[promoted_id], "llm_reviewed")
        self.assertEqual(report["safe_source_faithful_rows_scanned"], 1)
        self.assertEqual(report["safe_source_faithful_promoted"], 1)
        self.assertEqual(report["safe_source_faithful_promoted_written"], 1)

    def test_does_not_promote_safe_source_faithful_dangling_suffix_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)
            blocked_id = self._insert_needs_review_label(
                db_path,
                concept_id=ids["safe_concept_id"],
                ksa_id=ids["skipped_ksa_id"],
                source_text="요구사항을 파악할 수 있는 능력",
                label_text="요구사항을 파악할 수 있는",
                source_method="rule_based_short_label_candidate",
                confidence_score=0.73,
            )

            report = run_codex_judge(
                db_path=db_path,
                out=Path(tmp) / "judge.json",
                manual_seedpack_out=Path(tmp) / "manual.jsonl",
                min_confidence=0.75,
                min_ratio=0.35,
                max_ratio=0.98,
                manual_limit=20,
                dry_run=False,
                mark_skipped_needs_review=False,
                promote_safe_source_faithful_needs_review=True,
            )
            statuses = self._label_statuses(db_path)

        self.assertEqual(statuses[blocked_id], "needs_review")
        self.assertEqual(report["safe_source_faithful_rows_scanned"], 1)
        self.assertEqual(report["safe_source_faithful_promoted"], 0)
        self.assertIn("dangling_label_ending", report["safe_source_faithful_skipped_by_reason"])

    def test_create_repaired_label_candidate_without_overwriting_needs_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)
            source_text = "요구사항을 정확하게 파악하여 계획수립에 반영할 수 있는 능력"
            existing_id = self._insert_needs_review_label(
                db_path,
                concept_id=ids["safe_concept_id"],
                ksa_id=ids["skipped_ksa_id"],
                source_text=source_text,
                label_text="요구사항을 정확하게 파악하여 계획수립에 반영할 수 있는",
                source_method="rule_based_short_label_candidate",
                confidence_score=0.73,
            )

            report = run_codex_judge(
                db_path=db_path,
                out=Path(tmp) / "judge.json",
                manual_seedpack_out=Path(tmp) / "manual.jsonl",
                min_confidence=0.75,
                min_ratio=0.35,
                max_ratio=0.98,
                manual_limit=20,
                dry_run=False,
                mark_skipped_needs_review=False,
                create_repaired_label_candidates=True,
            )
            conn = connect(db_path)
            try:
                existing_status = conn.execute(
                    """
                    SELECT review_status
                    FROM ontology_concept_label_candidates
                    WHERE label_id = ?
                    """,
                    (existing_id,),
                ).fetchone()["review_status"]
                repaired = conn.execute(
                    """
                    SELECT label_text, source_method, review_status, evidence_text
                    FROM ontology_concept_label_candidates
                    WHERE source_method = 'llm_repaired_source_faithful_label'
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(existing_status, "needs_review")
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired["label_text"], "요구사항을 정확하게 파악하여 계획수립에 반영")
        self.assertEqual(repaired["review_status"], "llm_reviewed")
        self.assertIn(str(existing_id), repaired["evidence_text"])
        self.assertEqual(report["repaired_label_candidates"], 1)
        self.assertEqual(report["repaired_label_candidates_written"], 1)
        self.assertEqual(report["forbidden_statuses_written"], [])

    def test_repaired_label_candidate_blocks_too_short_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)
            self._insert_needs_review_label(
                db_path,
                concept_id=ids["safe_concept_id"],
                ksa_id=ids["skipped_ksa_id"],
                source_text="작성 능력",
                label_text="작성",
                source_method="rule_based_short_label_candidate",
                confidence_score=0.73,
            )

            report = run_codex_judge(
                db_path=db_path,
                out=Path(tmp) / "judge.json",
                manual_seedpack_out=Path(tmp) / "manual.jsonl",
                min_confidence=0.75,
                min_ratio=0.35,
                max_ratio=0.98,
                manual_limit=20,
                dry_run=False,
                mark_skipped_needs_review=False,
                create_repaired_label_candidates=True,
            )
            conn = connect(db_path)
            try:
                repaired_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM ontology_concept_label_candidates
                    WHERE source_method = 'llm_repaired_source_faithful_label'
                    """
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(repaired_count, 0)
        self.assertEqual(report["repaired_label_candidates"], 0)
        self.assertIn("repaired_label_too_short", report["repaired_label_skipped_by_reason"])

    def test_does_not_promote_already_short_needs_review_with_quality_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            ids = self._create_fixture(db_path)
            blocked_id = self._insert_needs_review_label(
                db_path,
                concept_id=ids["safe_concept_id"],
                ksa_id=ids["skipped_ksa_id"],
                source_text="ISO 9001",
                label_text="ISO 9001",
            )

            report = run_codex_judge(
                db_path=db_path,
                out=Path(tmp) / "judge.json",
                manual_seedpack_out=Path(tmp) / "manual.jsonl",
                min_confidence=0.75,
                min_ratio=0.35,
                max_ratio=0.98,
                manual_limit=20,
                dry_run=False,
                mark_skipped_needs_review=False,
                promote_safe_already_short_needs_review=True,
            )
            statuses = self._label_statuses(db_path)

        self.assertEqual(statuses[blocked_id], "needs_review")
        self.assertEqual(report["safe_already_short_rows_scanned"], 1)
        self.assertEqual(report["safe_already_short_promoted"], 0)
        self.assertEqual(report["safe_already_short_skipped"], 1)
        self.assertIn("quality_flag:digit_heavy", report["safe_already_short_skipped_by_reason"])

    def test_run_does_not_write_human_or_concept_definition_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            self._create_fixture(db_path)

            report = self._run_judge(tmp, db_path)
            conn = connect(db_path)
            try:
                label_statuses = {
                    str(row["review_status"])
                    for row in conn.execute(
                        "SELECT review_status FROM ontology_concept_label_candidates"
                    ).fetchall()
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
        self.assertFalse(label_statuses & FORBIDDEN_HUMAN_STATUSES)
        self.assertEqual(
            [(row["definition_status"], row["review_status"]) for row in concept_rows],
            [("missing", "raw"), ("missing", "raw")],
        )


if __name__ == "__main__":
    unittest.main()
