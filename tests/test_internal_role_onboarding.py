from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.onboard_internal_roles import (
    INPUT_SCHEMA,
    OnboardingValidationError,
    _canonical_json_sha256,
    _mapping_fingerprint,
    build_validation_report,
    check_candidate_packet,
    main,
    prepare_candidate_packet,
    validate_onboarding_input,
)
from ncs_mcp.builder_gold import load_internal_role_mapping_packet


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "examples" / "internal_role_onboarding.template.json"
SCHEMA = """
CREATE TABLE classifications (
  classification_id INTEGER PRIMARY KEY, major_code TEXT, major_name TEXT,
  middle_code TEXT, middle_name TEXT, small_code TEXT, small_name TEXT,
  sub_code TEXT, sub_name TEXT
);
CREATE TABLE competency_units (
  unit_code TEXT PRIMARY KEY, classification_id INTEGER,
  unit_name_refined TEXT, unit_name_raw TEXT, api_unit_name TEXT
);
CREATE TABLE competency_elements (
  element_id INTEGER PRIMARY KEY, unit_code TEXT,
  element_name_refined TEXT, element_name_raw TEXT, api_element_name TEXT
);
"""


class InternalRoleOnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "ncs.db"
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (1, "02", "경영", "01", "기획", "01", "인사", "01", "인사기획"),
            )
            connection.execute(
                "INSERT INTO competency_units VALUES (?, ?, ?, ?, ?)",
                ("0201010101", 1, None, "인력운영계획", None),
            )
            connection.execute(
                "INSERT INTO competency_elements VALUES (?, ?, ?, ?, ?)",
                (11, "0201010101", None, "인력 수요 예측", None),
            )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def valid_payload() -> dict:
        return {
            "schema": INPUT_SCHEMA,
            "organization_namespace": "acme-hr",
            "template_only": False,
            "roles": [
                {
                    "role_id": "hr-planner",
                    "role_name": "인사기획 담당",
                    "dept": "인사 부서",
                    "job_description": (
                        "조직 목표에 맞춰 중장기 인력 운영계획을 수립하고 대안을 검토한다."
                    ),
                    "tasks": ["중장기 인력 수요를 산정한다."],
                    "skills": ["인력 수요 예측", "직무 분석"],
                    "target_level": "NCS 4 수준",
                    "effective_date": "2026-01-01",
                    "source_ref": "role-catalog-v1",
                }
            ],
        }

    def test_checked_in_template_is_valid_but_not_production_eligible(self) -> None:
        payload = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        batch = validate_onboarding_input(payload)
        report = build_validation_report(batch)
        self.assertTrue(batch.template_only)
        self.assertTrue(report["ok"])
        self.assertFalse(report["production_eligible"])
        self.assertFalse(report["approval_claim"])
        self.assertFalse(report["db_writes"])
        self.assertEqual(
            report["pii_screening"]["method"],
            "heuristic_dlp_pattern_screen_v1",
        )
        self.assertIn("not proof", report["pii_screening"]["residual_risk"])

    def test_dept_jd_tasks_and_skills_feed_existing_role_contract(self) -> None:
        batch = validate_onboarding_input(self.valid_payload())
        role = batch.to_internal_roles()[0]
        self.assertIn("인사 부서", role.semantic_text)
        self.assertIn("중장기 인력", role.semantic_text)
        self.assertIn("인력 수요 예측", role.semantic_text)
        self.assertIn("중장기 인력 수요를 산정한다", role.semantic_text)
        self.assertEqual(role.provenance["department"], "인사 부서")

    def test_strict_validation_rejects_unknown_fields_duplicates_and_pii(self) -> None:
        for mutator in (
            lambda payload: payload["roles"][0].update(employee_name="홍길동"),
            lambda payload: payload["roles"][0].update(
                job_description="담당자 person@example.com에게 연락하여 역할 업무를 수행한다."
            ),
            lambda payload: payload["roles"].append(copy.deepcopy(payload["roles"][0])),
        ):
            payload = self.valid_payload()
            mutator(payload)
            with self.subTest(payload=payload), self.assertRaises(OnboardingValidationError):
                validate_onboarding_input(payload)

    def test_heuristic_dlp_rejects_reviewer_pii_reproductions(self) -> None:
        prohibited_values = (
            "역할 문의 전화는 +82 10-1234-5678이며 직무 설명에 포함되었다.",
            "외국인등록번호 900101-5123456을 직무 설명에 포함하면 안 된다.",
            "담당자 성명: 홍길동이 인사기획 역할을 현재 수행하고 있다.",
            "담당 사번: E-12345인 재직자가 인사기획 역할을 수행한다.",
            "담당자 생년월일: 1990-01-01인 재직자의 역할 정보이다.",
            "담당자 주소: 서울특별시 강남구 테헤란로 1을 역할에 기록했다.",
        )
        for value in prohibited_values:
            payload = self.valid_payload()
            payload["roles"][0]["job_description"] = value
            with self.subTest(value=value), self.assertRaises(OnboardingValidationError):
                validate_onboarding_input(payload)

    def test_template_requires_explicit_smoke_override(self) -> None:
        payload = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        batch = validate_onboarding_input(payload)
        with self.assertRaises(OnboardingValidationError):
            prepare_candidate_packet(batch, self.db_path)

        wrapper_out = Path(self.temp.name) / "template-wrapper.json"
        builder_out = Path(self.temp.name) / "template-builder.json"
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(
                [
                    "prepare",
                    "--input",
                    str(TEMPLATE),
                    "--db",
                    str(self.db_path),
                    "--out",
                    str(wrapper_out),
                    "--builder-out",
                    str(builder_out),
                ]
            )
        self.assertFalse(wrapper_out.exists())
        self.assertFalse(builder_out.exists())

    def test_prepare_and_check_are_candidate_only_and_read_only(self) -> None:
        batch = validate_onboarding_input(self.valid_payload())
        packet = prepare_candidate_packet(batch, self.db_path, limit=3)
        result = packet["mapping_packet"]["role_results"][0]
        self.assertEqual(result["alignment_candidates"][0]["ncs_target_key"], "02010101")
        self.assertIn(result["status"], {"candidate", "ambiguous", "unresolved"})
        self.assertTrue(packet["candidate_only"])
        self.assertFalse(packet["approval_claim"])
        self.assertFalse(packet["db_writes"])
        self.assertFalse(packet["neo4j_writes"])
        check = check_candidate_packet(packet, db_path=self.db_path)
        self.assertTrue(check["ok"], check["errors"])
        self.assertFalse(check["approval_claim"])
        self.assertEqual(check["source_db_sha256"], packet["source_db_sha256"])
        self.assertEqual(
            check["mapping_packet_sha256"], packet["mapping_packet_sha256"]
        )
        self.assertEqual(
            check["mapping_fingerprint_sha256"],
            check["recomputed_mapping_fingerprint_sha256"],
        )

    def test_packet_check_blocks_approval_or_write_tampering(self) -> None:
        batch = validate_onboarding_input(self.valid_payload())
        packet = prepare_candidate_packet(batch, self.db_path)
        packet["approval_claim"] = True
        packet["mapping_packet"]["provenance"]["db_writes"] = True
        packet["mapping_packet"]["role_results"][0]["status"] = "accepted"
        check = check_candidate_packet(packet, db_path=self.db_path)
        self.assertFalse(check["ok"])
        self.assertGreaterEqual(check["error_count"], 3)
        self.assertFalse(check["approval_claim"])
        self.assertFalse(check["db_writes"])

    def test_packet_check_enforces_production_eligibility_and_full_role_equality(self) -> None:
        batch = validate_onboarding_input(self.valid_payload())
        packet = prepare_candidate_packet(batch, self.db_path)
        packet["production_eligible"] = False
        production_check = check_candidate_packet(packet, db_path=self.db_path)
        self.assertFalse(production_check["ok"])
        self.assertTrue(
            any("production_eligible" in error for error in production_check["errors"])
        )

        packet = prepare_candidate_packet(batch, self.db_path)
        packet["mapping_packet"]["role_results"][0]["role"]["description"] += " 변조"
        packet["mapping_packet_sha256"] = _canonical_json_sha256(
            packet["mapping_packet"]
        )
        packet["mapping_fingerprint_sha256"] = _mapping_fingerprint(
            packet["mapping_packet"]
        )
        role_check = check_candidate_packet(packet, db_path=self.db_path)
        self.assertFalse(role_check["ok"])
        self.assertTrue(
            any("canonical source profile" in error for error in role_check["errors"])
        )

    def test_packet_check_recomputes_mapping_and_blocks_inner_tampering(self) -> None:
        batch = validate_onboarding_input(self.valid_payload())
        original = prepare_candidate_packet(batch, self.db_path)

        def target(packet: dict) -> None:
            packet["mapping_packet"]["role_results"][0]["alignment_candidates"][0][
                "ncs_target_key"
            ] = "99999999"

        def score(packet: dict) -> None:
            packet["mapping_packet"]["role_results"][0]["alignment_candidates"][0][
                "score"
            ] = 0.123456

        def evidence(packet: dict) -> None:
            packet["mapping_packet"]["role_results"][0]["alignment_candidates"][0][
                "evidence"
            ][0]["matches"][0]["ncs_text"] = "tampered evidence"

        for mutate in (target, score, evidence):
            packet = copy.deepcopy(original)
            mutate(packet)
            # Recalculate both wrapper hashes to prove the DB-bound
            # deterministic recomputation, not only the stored hash, catches it.
            packet["mapping_packet_sha256"] = _canonical_json_sha256(
                packet["mapping_packet"]
            )
            packet["mapping_fingerprint_sha256"] = _mapping_fingerprint(
                packet["mapping_packet"]
            )
            check = check_candidate_packet(packet, db_path=self.db_path)
            with self.subTest(mutation=mutate.__name__):
                self.assertFalse(check["ok"])
                self.assertTrue(
                    any("deterministic recomputation" in error for error in check["errors"]),
                    check["errors"],
                )

    def test_packet_check_is_bound_to_the_exact_source_db_file(self) -> None:
        batch = validate_onboarding_input(self.valid_payload())
        packet = prepare_candidate_packet(batch, self.db_path)
        other_db = Path(self.temp.name) / "other.db"
        other_db.write_bytes(self.db_path.read_bytes())
        connection = sqlite3.connect(other_db)
        try:
            connection.execute(
                "UPDATE classifications SET sub_name = ? WHERE classification_id = 1",
                ("변경된 인사 직무",),
            )
            connection.commit()
        finally:
            connection.close()
        check = check_candidate_packet(packet, db_path=other_db)
        self.assertFalse(check["ok"])
        self.assertNotEqual(check["source_db_sha256"], packet["source_db_sha256"])
        self.assertTrue(
            any("source_db_sha256" in error for error in check["errors"]),
            check["errors"],
        )

    def test_cli_builder_out_is_loadable_by_existing_builder_contract(self) -> None:
        input_path = Path(self.temp.name) / "roles.json"
        wrapper_out = Path(self.temp.name) / "wrapper.json"
        builder_out = Path(self.temp.name) / "builder_roles.json"
        input_path.write_text(
            json.dumps(self.valid_payload(), ensure_ascii=False), encoding="utf-8"
        )
        with redirect_stdout(io.StringIO()):
            exit_code = main(
                [
                    "prepare",
                    "--input",
                    str(input_path),
                    "--db",
                    str(self.db_path),
                    "--out",
                    str(wrapper_out),
                    "--builder-out",
                    str(builder_out),
                ]
            )
        self.assertEqual(exit_code, 0)
        roles, candidates = load_internal_role_mapping_packet(builder_out)
        self.assertEqual(len(roles), 1)
        self.assertGreaterEqual(len(candidates), 1)
        raw_builder = json.loads(builder_out.read_text(encoding="utf-8"))
        self.assertEqual(raw_builder["schema"], "ncs_internal_role_mapping_packet_v1")
        self.assertTrue(raw_builder["candidate_only"])
        self.assertFalse(raw_builder["approval_claim"])
        self.assertFalse(raw_builder["db_writes"])
        wrapper = json.loads(wrapper_out.read_text(encoding="utf-8"))
        self.assertEqual(wrapper["builder_artifact"]["role_count"], 1)
        self.assertFalse(wrapper["builder_artifact"]["approval_claim"])


if __name__ == "__main__":
    unittest.main()
