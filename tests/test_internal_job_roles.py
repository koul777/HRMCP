from __future__ import annotations

import copy
import unittest

from ncs_mcp.internal_job_roles import (
    ALIGNMENT_STATUSES,
    ContractValidationError,
    InternalJobRole,
    RoleAlignmentCandidate,
    public_safe_response,
    public_safe_projection,
)


class InternalJobRoleContractTests(unittest.TestCase):
    def make_role(self, **overrides):
        payload = {
            "organization_namespace": "Acme HR",
            "role_id": "people-partner",
            "display_name": "People Partner",
            "aliases": ["HRBP", "People  Partner"],
            "description": "Supports workforce planning",
            "duties": ["Plan workforce needs", "Advise leaders"],
            "target_level": "senior",
            "source": "internal_role_catalog",
            "effective_date": "2026-01-01",
        }
        payload.update(overrides)
        return InternalJobRole.from_mapping(payload)

    def test_identity_is_tenant_scoped(self):
        first = self.make_role()
        second = self.make_role(organization_namespace="Other HR")
        self.assertNotEqual(first.gold_id, second.gold_id)
        self.assertEqual(first.canonical_organization_namespace, "acme-hr")
        self.assertEqual(first.canonical_role_id, "people-partner")

    def test_gold_id_is_deterministic_and_content_changes_do_not_change_identity(self):
        first = self.make_role()
        repeat = self.make_role()
        edited = self.make_role(description="A different description")
        self.assertEqual(first.gold_id, repeat.gold_id)
        self.assertEqual(first.gold_id, edited.gold_id)

    def test_normalization_does_not_mutate_raw_input(self):
        payload = {
            "organization_namespace": "Org",
            "role_id": "r-1",
            "display_name": "  HR  Lead ",
            "aliases": ["  People Lead  "],
            "duties": ["  Coach   managers "],
        }
        before = copy.deepcopy(payload)
        role = InternalJobRole.from_mapping(payload)
        self.assertEqual(payload, before)
        self.assertEqual(role.display_name, "  HR  Lead ")
        self.assertIn("hr lead", role.normalized_semantic_text)
        payload["aliases"].append("new")
        self.assertEqual(role.aliases, ("  People Lead  ",))

    def test_empty_identity_and_personal_fields_are_rejected(self):
        with self.assertRaises(ContractValidationError):
            self.make_role(role_id=" ")
        with self.assertRaises(ContractValidationError):
            self.make_role(employee_id="E-99")
        with self.assertRaises(ContractValidationError):
            self.make_role(provenance={"contact_email": "person@example.com"})
        with self.assertRaises(ContractValidationError):
            self.make_role(provenance={"중첩": {"성명": "홍길동"}})
        for field in ("user_id", "user_name", "reviewer_id", "social_security"):
            with self.assertRaises(ContractValidationError):
                self.make_role(provenance={field: "sensitive"})


class AlignmentCandidateContractTests(unittest.TestCase):
    def make_candidate(self, **overrides):
        payload = {
            "role_gold_id": "ijr_abc",
            "ncs_target_type": "competency_unit",
            "ncs_target_key": "0201010101",
            "score": 0.82,
            "method": "semantic_embedding",
            "model": "local-model-v1",
            "evidence": [{"source": "role_description", "text": "workforce planning"}],
            "status": "candidate",
            "provenance": {"source": "alignment_run", "run_id": "run-1"},
        }
        payload.update(overrides)
        return RoleAlignmentCandidate(**payload)

    def test_status_is_limited_to_non_approval_states(self):
        self.assertEqual(ALIGNMENT_STATUSES, {"candidate", "review_required", "ambiguous", "unresolved"})
        for status in ("human_reviewed", "accepted", "reviewed"):
            with self.assertRaises(ContractValidationError):
                self.make_candidate(status=status)

    def test_evidence_is_preserved_and_copied(self):
        evidence = [{"source": "criterion", "text": "planning"}]
        candidate = self.make_candidate(evidence=evidence)
        evidence[0]["text"] = "changed"
        self.assertEqual(candidate.evidence[0]["text"], "planning")
        self.assertEqual(candidate.to_dict()["evidence"][0]["source"], "criterion")

    def test_nested_korean_personal_evidence_is_rejected(self):
        with self.assertRaises(ContractValidationError):
            self.make_candidate(evidence=[{"근거": {"사번": "E-1"}}])
        with self.assertRaises(ContractValidationError):
            self.make_candidate(provenance={"연락처": "010-1234-5678"})

    def test_public_projection_redacts_personal_fields_and_keeps_provenance(self):
        role = InternalJobRole.from_mapping(
            {
                "organization_namespace": "Org",
                "role_id": "r-1",
                "display_name": "Analyst",
                "provenance": {"source": "catalog", "record_ref": "r-1"},
            }
        )
        candidate = self.make_candidate()
        public = public_safe_projection(
            {
                "safe": "value",
                "employee_id": "E-1",
                "nested": {
                    "이메일": "person@example.com",
                    "주민등록번호": "900101-1234567",
                    "user_id": "U-1",
                    "social_security": "900101-1234567",
                    "source": "criterion",
                },
            }
        )
        self.assertEqual(public, {"safe": "value", "nested": {"source": "criterion"}})
        self.assertEqual(
            public_safe_projection({"직무이름": "인사담당자", "이름": "홍길동"}),
            {"직무이름": "인사담당자"},
        )
        projection = candidate.to_public_dict()
        self.assertEqual(projection["provenance"]["source"], "alignment_run")
        self.assertNotIn("employee_id", projection)

    def test_public_response_contains_provenance(self):
        role = InternalJobRole.from_mapping(
            {"organization_namespace": "Org", "role_id": "r-1", "display_name": "Analyst"}
        )
        candidate = self.make_candidate(role_gold_id=role.gold_id)
        response = public_safe_response(role, [candidate])
        self.assertEqual(response["role"]["gold_id"], role.gold_id)
        self.assertEqual(response["alignment_candidates"][0]["status"], "candidate")
        self.assertEqual(response["provenance"]["candidate_count"], 1)

    def test_public_response_rejects_cross_tenant_candidate_for_object_and_mapping(self):
        role = InternalJobRole.from_mapping(
            {"organization_namespace": "Org", "role_id": "r-1", "display_name": "Analyst"}
        )
        candidate = self.make_candidate()
        with self.assertRaises(ContractValidationError):
            public_safe_response(role, [candidate])
        with self.assertRaises(ContractValidationError):
            public_safe_response(role, [candidate.to_dict()])


if __name__ == "__main__":
    unittest.main()
