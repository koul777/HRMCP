from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ncs_mcp.hrd_guide_reference import (
    BUILTIN_HRD_GUIDE_PAGE_COUNT,
    BUILTIN_HRD_GUIDE_SOURCE_HASH_SHA256,
    HRD_GUIDE_REFERENCE_SCHEMA,
    build_hrd_guide_reference_index,
    fallback_hrd_guide_reference_index,
    preprocess_hrd_guide_reference,
)
from ncs_mcp.hrd_guide_prompt_coverage import (
    NEEDS_CONTEXT_CONTRACT_SCHEMA,
    PROMPT_COVERAGE_SCHEMA,
    build_hrd_guide_prompt_coverage_report,
    write_hrd_guide_prompt_coverage_markdown,
)


SAMPLE_GUIDE = """---
title: NCS HRD guide sample
pages: 2
---

<!-- page: 057 -->
## Page 057 - 교육훈련체계 구축

ChatGPT 프롬프트 예시: 노무관리 담당자가 인사기획으로 전환하기 위한 교육훈련체계를 수립해줘.
교육과정 조사는 과정명만이 아니라 목적, 대상, 내용, 시간, 운영방식을 함께 본다.

<!-- page: 078 -->
## Page 078 - C2-1 교육훈련체계도

C2-1 체계도는 직무, 수준, 교육유형, 필수/선택, 운영방식으로 정리한다.
"""


class HrdGuideReferenceTests(unittest.TestCase):
    def test_fallback_keeps_non_scoring_framework_reference_provenance(self) -> None:
        index = fallback_hrd_guide_reference_index()

        self.assertEqual(
            index["source"]["source_hash_sha256"],
            BUILTIN_HRD_GUIDE_SOURCE_HASH_SHA256,
        )
        self.assertEqual(index["source"]["page_count"], BUILTIN_HRD_GUIDE_PAGE_COUNT)
        self.assertFalse(index["source"]["available"])
        self.assertTrue(index["policy"]["not_source_training_data"])
        self.assertEqual(index["policy"]["scoring_use"], "validation_rubric_only")

    def test_build_index_keeps_framework_reference_policy(self) -> None:
        raw_bytes = SAMPLE_GUIDE.encode("utf-8")
        index = build_hrd_guide_reference_index(
            source_path=Path("source.md"),
            project_copy_path=Path("docs/reference/ncs_hrd_guide_codex_readable.md"),
            text=SAMPLE_GUIDE,
            raw_bytes=raw_bytes,
            encoding="utf-8",
            generated_at="2026-06-18T00:00:00+00:00",
        )

        self.assertEqual(index["schema"], HRD_GUIDE_REFERENCE_SCHEMA)
        self.assertEqual(index["policy"]["reference_role"], "framework_reference")
        self.assertTrue(index["policy"]["not_source_training_data"])
        self.assertEqual(index["source"]["page_count"], 2)
        self.assertIn("prompt_scenario_templates", index)
        self.assertGreaterEqual(len(index["prompt_scenario_templates"]), 9)
        template_ids = {item["id"] for item in index["prompt_scenario_templates"]}
        self.assertIn("job_structure_mapping", template_ids)
        self.assertIn("ncs_mapping_evidence_summary", template_ids)
        self.assertIn("training_course_inventory_table", template_ids)
        self.assertIn("internal_training_intake_questionnaire", template_ids)
        self.assertIn("job_course_mapping_framework", template_ids)
        self.assertIn("course_ksa_alignment", template_ids)
        trace_checks = {
            item["check"] for item in index["guide_trace_contract"]["checks"]
        }
        self.assertSetEqual(
            trace_checks,
            {
                "job_scope",
                "task_ksa",
                "course_link",
                "required_optional",
                "level_delivery",
                "human_review",
            },
        )
        blocker_ids = {item["id"] for item in index["acceptance_gates"]["blockers"]}
        self.assertIn("missing_query_route_contract", blocker_ids)
        self.assertIn("guide_used_as_source_training_data", blocker_ids)
        warning_ids = {item["id"] for item in index["acceptance_gates"]["warnings"]}
        self.assertIn("unknown_or_not_requested_facility", warning_ids)

    def test_preprocess_writes_project_reference_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "downloaded.md"
            reference_dir = root / "docs" / "reference"
            index_path = reference_dir / "index.json"
            markdown_path = reference_dir / "guide.md"
            chunks_path = reference_dir / "chunks.jsonl"
            source.write_text(SAMPLE_GUIDE, encoding="utf-8")

            result = preprocess_hrd_guide_reference(
                source_path=source,
                reference_dir=reference_dir,
                index_path=index_path,
                markdown_path=markdown_path,
                chunks_path=chunks_path,
            )

            self.assertTrue(result["ok"])
            self.assertTrue((reference_dir / "ncs_hrd_guide_codex_readable.md").exists())
            self.assertTrue(index_path.exists())
            self.assertTrue(markdown_path.exists())
            self.assertTrue(chunks_path.exists())
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["schema"], HRD_GUIDE_REFERENCE_SCHEMA)
            self.assertEqual(index["source"]["page_count"], 2)
            self.assertIn("acceptance_gates", index)
            self.assertNotIn(str(root), index_path.read_text(encoding="utf-8"))
            self.assertGreater(result["chunk_count"], 0)
            chunk_text = chunks_path.read_text(encoding="utf-8")
            self.assertIn("framework_reference_only", chunk_text)
            self.assertIn('"scoring_allowed": false', chunk_text)

    def test_no_copy_source_indexes_requested_source_not_stale_project_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference_dir = root / "docs" / "reference"
            reference_dir.mkdir(parents=True)
            stale_copy = reference_dir / "ncs_hrd_guide_codex_readable.md"
            stale_copy.write_text(SAMPLE_GUIDE.replace("Page 057", "Page 001"), encoding="utf-8")
            fresh_source = root / "fresh.md"
            fresh_source.write_text(SAMPLE_GUIDE, encoding="utf-8")
            index_path = reference_dir / "index.json"
            markdown_path = reference_dir / "guide.md"
            chunks_path = reference_dir / "chunks.jsonl"

            result = preprocess_hrd_guide_reference(
                source_path=fresh_source,
                reference_dir=reference_dir,
                index_path=index_path,
                markdown_path=markdown_path,
                chunks_path=chunks_path,
                copy_source=False,
            )

            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertFalse(result["copied_source"])
            self.assertEqual(index["source"]["page_count"], 2)
            self.assertIn("<external>", index["source"]["original_path"])
            self.assertIn("<external>", index["source"]["project_copy_path"])
            self.assertIn("교육훈련체계 구축", markdown_path.read_text(encoding="utf-8"))

    def test_prompt_coverage_report_checks_templates_and_writes_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "index.json"
            markdown_path = root / "coverage.md"
            index = build_hrd_guide_reference_index(
                source_path=Path("source.md"),
                project_copy_path=Path("docs/reference/ncs_hrd_guide_codex_readable.md"),
                text=SAMPLE_GUIDE,
                raw_bytes=SAMPLE_GUIDE.encode("utf-8"),
                encoding="utf-8",
                generated_at="2026-06-18T00:00:00+00:00",
            )
            index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

            report = build_hrd_guide_prompt_coverage_report(
                index_path=index_path,
                example_limit=3,
            )
            write_hrd_guide_prompt_coverage_markdown(report, markdown_path)

            self.assertEqual(report["schema"], PROMPT_COVERAGE_SCHEMA)
            self.assertTrue(report["ok"])
            self.assertEqual(report["summary"]["template_failed"], 0)
            self.assertGreaterEqual(report["summary"]["template_passed"], 3)
            self.assertGreaterEqual(report["summary"]["template_needs_context"], 1)
            self.assertIn("template_needs_context_ids", report["summary"])
            self.assertEqual(
                report["needs_context_contract"]["schema"],
                NEEDS_CONTEXT_CONTRACT_SCHEMA,
            )
            self.assertEqual(report["needs_context_contract"]["status"], "needs_context")
            self.assertIn(
                "current_query",
                report["needs_context_contract"]["missing_param_counts"],
            )
            self.assertLessEqual(report["summary"]["prompt_example_total"], 3)
            self.assertIn("section_counts", report["summary"])
            self.assertIn("training_system_prompt_total", report["summary"])
            warning_codes = {item["code"] for item in report["warnings"]}
            self.assertIn("prompt_templates_need_user_context", warning_codes)
            needs_context_warning = next(
                item for item in report["warnings"]
                if item["code"] == "prompt_templates_need_user_context"
            )
            self.assertEqual(needs_context_warning["contract_schema"], NEEDS_CONTEXT_CONTRACT_SCHEMA)
            self.assertIn("context_requirement_codes", needs_context_warning)
            annual = next(
                item for item in report["template_checks"]
                if item["template_id"] == "annual_operation_plan_draft"
            )
            self.assertIn("current_query", annual["allowed_missing_params"])
            self.assertEqual(annual["context_requirements"][0]["code"], "job_scope_required")
            self.assertEqual(annual["context_requirements"][0]["guide_stage"], "C1-1")
            self.assertTrue(markdown_path.exists())
            markdown_text = markdown_path.read_text(encoding="utf-8")
            self.assertIn("HRD Guide Prompt Coverage", markdown_text)
            self.assertIn("template_needs_context", markdown_text)
            self.assertIn("prompt_templates_need_user_context", markdown_text)
            self.assertIn("Needs Context Contract", markdown_text)


if __name__ == "__main__":
    unittest.main()
