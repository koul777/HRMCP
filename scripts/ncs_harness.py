from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.collect_api import (
    collect_elements_api,
    collect_sqf_api,
    collect_standard_api,
    collect_subd_api,
)
from ncs_mcp.collect_sqf_library import collect_sqf_library
from ncs_mcp.config import load_settings
from ncs_mcp.db import connect, initialize_database
from ncs_mcp.evaluation import run_evaluation
from ncs_mcp.handoff import export_handoff_package
from ncs_mcp.ontology import build_sqf_mapping_candidates
from ncs_mcp.preprocess_excel import preprocess_excel
from ncs_mcp.preprocess_sqf_documents import preprocess_sqf_documents
from ncs_mcp.quality import run_quality_checks
from ncs_mcp.refinement import parse_csv, run_refinement_harness
from ncs_mcp.server import get_competency_units, get_unit_structure
from ncs_mcp.sqf_precision_matching import build_sqf_chunk_job_level_matches
from ncs_mcp.sqf_sqlite import build_sqf_sqlite_model, sqf_model_summary


CORE_TABLES = [
    "raw_excel_rows",
    "classifications",
    "competency_units",
    "competency_elements",
    "performance_criteria",
    "ksa_items",
    "element_criteria_ksa_links",
    "api_raw_responses",
    "api_competency_units",
    "sqf_duties",
    "sqf_ncs_matches",
    "sqf_library_posts",
    "sqf_library_files",
    "sqf_document_sources",
    "sqf_framework_concepts",
    "sqf_industry_sectors",
    "sqf_jobs_normalized",
    "sqf_levels",
    "sqf_job_levels_normalized",
    "sqf_recognition_evidence",
    "sqf_document_assets",
    "sqf_document_pages",
    "sqf_document_chunks",
    "sqf_chunk_job_level_matches",
    "sqf_document_evidence_links",
    "quality_issues",
    "refinement_jobs",
]


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def table_counts(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    conn = connect(db_path)
    initialize_database(conn)
    counts = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in CORE_TABLES
    }
    conn.close()
    return counts


def inspect_project() -> dict[str, Any]:
    settings = load_settings()
    db_counts = table_counts(settings.db_path)
    payload: dict[str, Any] = {
        "root": str(ROOT),
        "excel_path": str(settings.excel_path) if settings.excel_path else None,
        "excel_exists": bool(settings.excel_path and settings.excel_path.exists()),
        "db_path": str(settings.db_path),
        "db_exists": settings.db_path.exists(),
        "reports_dir": str(settings.reports_dir),
        "service_key_present": bool(settings.service_key),
        "sqf_service_key_present": bool(settings.sqf_service_key),
        "counts": db_counts,
    }
    if settings.db_path.exists():
        conn = connect(settings.db_path)
        initialize_database(conn)
        payload["unit_api_status"] = {
            row["api_match_status"]: row["count"]
            for row in conn.execute(
                """
                SELECT api_match_status, COUNT(*) AS count
                FROM competency_units
                GROUP BY api_match_status
                """
            )
        }
        payload["element_api_status"] = {
            row["api_match_status"]: row["count"]
            for row in conn.execute(
                """
                SELECT api_match_status, COUNT(*) AS count
                FROM competency_elements
                GROUP BY api_match_status
                """
            )
        }
        payload["missing_duty_definitions"] = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM classifications
                WHERE duty_def_api IS NULL OR TRIM(duty_def_api) = ''
                """
            ).fetchone()[0]
        )
        payload["ontology_status"] = {
            "sqf_ncs_matches": int(
                conn.execute("SELECT COUNT(*) FROM sqf_ncs_matches").fetchone()[0]
            ),
            "sqf_ncs_reviewed_matches": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sqf_ncs_matches
                    WHERE review_status IN ('human_reviewed', 'reviewed', 'accepted')
                    """
                ).fetchone()[0]
            ),
            "management_support_sqf_duties": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sqf_duties
                    WHERE ncs_lclas_cd = '02'
                      AND sqf_field_name = '경영관리'
                      AND job_name = '경영지원'
                    """
                ).fetchone()[0]
            ),
        }
        payload["refinement_status"] = {
            "jobs": int(conn.execute("SELECT COUNT(*) FROM refinement_jobs").fetchone()[0]),
            "pending_jobs": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM refinement_jobs
                    WHERE review_status = 'review_required'
                    """
                ).fetchone()[0]
            ),
            "applied_jobs": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM refinement_jobs
                    WHERE review_status = 'applied'
                    """
                ).fetchone()[0]
            ),
        }
        conn.close()
    return payload


def require_ready_for_preprocess(reset: bool, allow_append: bool) -> None:
    settings = load_settings()
    if settings.excel_path is None or not settings.excel_path.exists():
        raise SystemExit("NCS_EXCEL_PATH is missing or does not exist.")
    if settings.db_path.exists() and not reset and not allow_append:
        counts = table_counts(settings.db_path)
        if counts.get("raw_excel_rows", 0) > 0:
            raise SystemExit(
                "Refusing to append to an existing DB. Use --reset or --allow-append."
            )


def run_smoke_check(
    major_code: str = "02",
    middle_code: str = "02",
    small_code: str = "02",
    sub_code: str = "01",
) -> dict[str, Any]:
    units = get_competency_units(
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        limit=50,
    )["units"]
    if not units:
        raise SystemExit("Smoke check failed: no competency units returned.")
    structure = get_unit_structure(units[0]["unit_code"])
    if "error" in structure:
        raise SystemExit(f"Smoke check failed: {structure}")
    elements = structure["elements"]
    criteria_total = sum(len(item["performance_criteria"]) for item in elements)
    ksa_total = sum(len(item["ksa"]) for item in elements)
    if not elements or not criteria_total or not ksa_total:
        raise SystemExit("Smoke check failed: incomplete unit hierarchy.")
    return {
        "classification": {
            "major_code": major_code,
            "middle_code": middle_code,
            "small_code": small_code,
            "sub_code": sub_code,
        },
        "unit_count": len(units),
        "sample_unit": structure["unit"]["unit_code"],
        "sample_unit_name": structure["unit"]["unit_name"],
        "duty_definition_present": bool(
            structure["unit"]["classification"].get("duty_definition")
        ),
        "sample_elements": len(elements),
        "sample_criteria": criteria_total,
        "sample_ksa": ksa_total,
        "element_api_statuses": sorted(
            {item.get("api_match_status") for item in elements if item.get("api_match_status")}
        ),
    }


def plan_element_batches(batch_size: int, concurrency: int) -> dict[str, Any]:
    settings = load_settings()
    conn = connect(settings.db_path)
    initialize_database(conn)
    total = int(conn.execute("SELECT COUNT(*) FROM competency_elements").fetchone()[0])
    remaining = int(
        conn.execute(
            "SELECT COUNT(*) FROM competency_elements WHERE api_match_status != 'matched'"
        ).fetchone()[0]
    )
    conn.close()
    command = (
        "python src\\ncs_mcp\\collect_api.py --mode elements "
        f"--element-limit {batch_size} --only-uncollected --timeout 90 "
        f"--concurrency {concurrency} --max-retries 2"
    )
    return {
        "total_elements": total,
        "remaining_elements": remaining,
        "batch_size": batch_size,
        "concurrency": concurrency,
        "estimated_batches": math.ceil(remaining / batch_size) if batch_size else 0,
        "repeat_command": command,
    }


def add_issue(issues: list[dict[str, str]], severity: str, check: str, detail: str) -> None:
    issues.append({"severity": severity, "check": check, "detail": detail})


def scan_text_files() -> list[Path]:
    candidates: list[Path] = []
    for folder in [ROOT, ROOT / "src", ROOT / "tests", ROOT / "scripts", ROOT / "docs"]:
        if not folder.exists():
            continue
        for path in folder.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".py", ".md", ".toml", ".txt"}:
                candidates.append(path)
    return candidates


def lint_repo(strict: bool = False) -> dict[str, Any]:
    settings = load_settings()
    issues: list[dict[str, str]] = []

    required_paths = [
        ROOT / "AGENTS.md",
        ROOT / "ARCHITECTURE.md",
        ROOT / "docs" / "HARNESS_ENGINEERING.md",
        ROOT / "docs" / "NCS_MCP_PRD.md",
        ROOT / "src" / "ncs_mcp" / "db.py",
        ROOT / "src" / "ncs_mcp" / "preprocess_excel.py",
        ROOT / "src" / "ncs_mcp" / "collect_api.py",
        ROOT / "src" / "ncs_mcp" / "server.py",
    ]
    for path in required_paths:
        if not path.exists():
            add_issue(issues, "error", "required_path", f"Missing {path.relative_to(ROOT)}")

    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8") if (ROOT / "AGENTS.md").exists() else ""
    for expected in ["ARCHITECTURE.md", "HARNESS_ENGINEERING.md", "ncs_harness.py"]:
        if expected not in agents:
            add_issue(issues, "warning", "agents_map", f"AGENTS.md does not reference {expected}")

    forbidden_imports = {
        "src/ncs_mcp/server.py": ["requests", "openpyxl"],
        "src/ncs_mcp/preprocess_excel.py": ["requests", "mcp.server"],
        "src/ncs_mcp/collect_api.py": ["openpyxl", "mcp.server"],
        "src/ncs_mcp/db.py": ["requests", "openpyxl", "mcp.server"],
    }
    for rel_path, forbidden in forbidden_imports.items():
        path = ROOT / rel_path
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                add_issue(
                    issues,
                    "error",
                    "module_boundary",
                    f"{rel_path} must not depend on {token}",
                )

    secret_values = {value for value in [settings.service_key, settings.sqf_service_key] if value}
    for secret_value in secret_values:
        for path in scan_text_files():
            if secret_value in path.read_text(encoding="utf-8", errors="ignore"):
                add_issue(
                    issues,
                    "error",
                    "secret_scan",
                    f"Service key appears in {path.relative_to(ROOT)}",
                )
    if settings.db_path.exists():
        conn = connect(settings.db_path)
        initialize_database(conn)
        raw_count = int(conn.execute("SELECT COUNT(*) FROM raw_excel_rows").fetchone()[0])
        link_count = int(
            conn.execute("SELECT COUNT(*) FROM element_criteria_ksa_links").fetchone()[0]
        )
        if raw_count != link_count:
            add_issue(
                issues,
                "error",
                "db_integrity",
                f"raw_excel_rows={raw_count} but links={link_count}",
            )
        ksa_count = int(conn.execute("SELECT COUNT(*) FROM ksa_items").fetchone()[0])
        if raw_count and not ksa_count:
            add_issue(issues, "error", "db_integrity", "DB has raw rows but no KSA rows")
        unmatched_units = int(
            conn.execute(
                "SELECT COUNT(*) FROM competency_units WHERE api_match_status != 'matched'"
            ).fetchone()[0]
        )
        if strict and unmatched_units:
            add_issue(
                issues,
                "error",
                "api_coverage",
                f"{unmatched_units} competency units are not API matched",
            )
        conn.close()
    elif strict:
        add_issue(issues, "error", "db_integrity", "SQLite DB does not exist")

    errors = [issue for issue in issues if issue["severity"] == "error"]
    warnings = [issue for issue in issues if issue["severity"] == "warning"]
    return {
        "ok": not errors,
        "errors": len(errors),
        "warnings": len(warnings),
        "issues": issues,
    }


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings()
    summary: dict[str, Any] = {"stages": {}}
    if args.preprocess:
        require_ready_for_preprocess(args.reset, args.allow_append)
        summary["stages"]["preprocess"] = preprocess_excel(
            excel_path=settings.excel_path,
            db_path=settings.db_path,
            reports_dir=settings.reports_dir,
            reset=args.reset,
            sheets=set(args.sheets.split(",")) if args.sheets else None,
            max_rows=args.max_rows,
        )
    if args.quality:
        summary["stages"]["quality"] = run_quality_checks(settings.db_path, settings.reports_dir)
    if args.api_standards:
        summary["stages"]["api_standards"] = collect_standard_api(
            settings.db_path,
            settings.reports_dir,
            settings.service_key or "",
            timeout=args.timeout,
        )
    if args.api_subd:
        summary["stages"]["api_subd"] = collect_subd_api(
            settings.db_path,
            settings.reports_dir,
            settings.service_key or "",
            timeout=args.timeout,
        )
    if args.api_elements_hr:
        summary["stages"]["api_elements_hr"] = collect_elements_api(
            settings.db_path,
            settings.reports_dir,
            settings.service_key or "",
            timeout=args.timeout,
            major_code="02",
            middle_code="02",
            small_code="02",
            sub_code="01",
        )
    if args.api_sqf:
        summary["stages"]["api_sqf"] = collect_sqf_api(
            settings.db_path,
            settings.reports_dir,
            settings.sqf_service_key or "",
            timeout=args.timeout,
            major_code=args.sqf_major_code,
            major_limit=args.sqf_major_limit,
        )
    if args.collect_sqf_library:
        summary["stages"]["collect_sqf_library"] = collect_sqf_library(
            settings.db_path,
            raw_dir=ROOT / "data" / "raw" / "sqf_docs",
            start_page=args.sqf_library_start_page,
            end_page=args.sqf_library_end_page,
            download=args.download_sqf_library,
            timeout=args.timeout,
            overwrite=args.overwrite_sqf_library,
            delay=args.sqf_library_delay,
        )
    if args.build_sqf_sqlite_model:
        summary["stages"]["build_sqf_sqlite_model"] = build_sqf_sqlite_model(settings.db_path)
    if args.preprocess_sqf_documents:
        summary["stages"]["preprocess_sqf_documents"] = preprocess_sqf_documents(
            settings.db_path,
            extracted_dir=ROOT / "data" / "raw" / "sqf_docs_extracted",
            chunk_chars=args.sqf_chunk_chars,
            overlap_chars=args.sqf_overlap_chars,
            ocr_empty=args.sqf_ocr_empty,
            ocr_lang=args.sqf_ocr_lang,
            ocr_dpi=args.sqf_ocr_dpi,
            ocr_max_pages=args.sqf_ocr_max_pages,
            only_unprocessed=args.sqf_only_unprocessed,
        )
    if args.build_sqf_precision_matches:
        summary["stages"]["build_sqf_precision_matches"] = build_sqf_chunk_job_level_matches(
            settings.db_path,
            min_score=args.sqf_precision_min_score,
            max_matches_per_chunk=args.sqf_precision_max_matches_per_chunk,
            asset_id=args.sqf_precision_asset_id,
        )
    if args.build_sqf_mappings:
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            summary["stages"]["build_sqf_mappings"] = build_sqf_mapping_candidates(
                conn,
                mvp_only=not args.all_sqf_mappings,
                major_code=args.mapping_major_code,
                keyword=args.mapping_keyword,
                source_key=args.mapping_source_key,
                limit_per_duty=args.mapping_limit_per_duty,
                duty_limit=args.mapping_duty_limit,
            )
        finally:
            conn.close()
    if args.refine:
        summary["stages"]["refine_generate"] = run_refinement_harness(
            settings.db_path,
            action="generate",
            issue_types=parse_csv(args.refine_issue_types),
            target_types=parse_csv(args.refine_target_types),
            limit=args.refine_limit,
        )
    if args.apply_refinements:
        summary["stages"]["refine_apply"] = run_refinement_harness(
            settings.db_path,
            action="apply",
            target_types=parse_csv(args.refine_target_types),
            limit=args.refine_limit,
        )
    if args.smoke:
        os.environ["NCS_DB_PATH"] = str(settings.db_path)
        summary["stages"]["smoke"] = run_smoke_check()
    if args.lint:
        summary["stages"]["lint"] = lint_repo(strict=args.strict_lint)
    summary["snapshot"] = inspect_project()
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NCS MCP project harness.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("inspect", help="Show environment, path, DB, and API status.")

    smoke = subparsers.add_parser("smoke", help="Run MCP-layer smoke checks.")
    smoke.add_argument("--major-code", default="02")
    smoke.add_argument("--middle-code", default="02")
    smoke.add_argument("--small-code", default="02")
    smoke.add_argument("--sub-code", default="01")

    plan = subparsers.add_parser("plan-elements", help="Plan /NCS006 batch collection.")
    plan.add_argument("--batch-size", type=int, default=8000)
    plan.add_argument("--concurrency", type=int, default=8)

    dashboard = subparsers.add_parser("dashboard", help="Print the dashboard command.")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)

    mappings = subparsers.add_parser(
        "build-sqf-mappings",
        help="Build NCS-SQF ontology mapping candidates for dashboard review.",
    )
    mappings.add_argument("--all-sqf", action="store_true")
    mappings.add_argument("--major-code")
    mappings.add_argument("--keyword")
    mappings.add_argument("--source-key")
    mappings.add_argument("--limit-per-duty", type=int, default=10)
    mappings.add_argument("--duty-limit", type=int, default=5000)

    export_package = subparsers.add_parser(
        "export-package",
        help="Create a handoff package with schema, dictionary, sample queries, and optional DB.",
    )
    export_package.add_argument("--out", default=str(ROOT / "exports" / "ncs_sqf_output"))
    export_package.add_argument(
        "--db-mode",
        choices=["none", "copy", "hardlink"],
        default="none",
        help="none writes docs only; hardlink creates data/db/ncs_sqf.sqlite without full copy; copy makes an independent DB file.",
    )
    export_package.add_argument("--zip", action="store_true")

    sqf_library = subparsers.add_parser(
        "collect-sqf-library",
        help="Collect SQF library report metadata and optional attachment files.",
    )
    sqf_library.add_argument("--start-page", type=int, default=0)
    sqf_library.add_argument("--end-page", type=int, default=10)
    sqf_library.add_argument("--download", action="store_true")
    sqf_library.add_argument("--raw-dir", type=Path, default=ROOT / "data" / "raw" / "sqf_docs")
    sqf_library.add_argument("--overwrite", action="store_true")
    sqf_library.add_argument("--timeout", type=int, default=30)
    sqf_library.add_argument("--delay", type=float, default=0.2)

    sqf_model = subparsers.add_parser(
        "build-sqf-sqlite-model",
        help="Build normalized SQF ontology tables from SQF API and document metadata.",
    )
    sqf_model.add_argument("--summary", action="store_true")

    sqf_docs = subparsers.add_parser(
        "preprocess-sqf-documents",
        help="Extract SQF PDF/ZIP report text into SQLite pages and chunks.",
    )
    sqf_docs.add_argument("--extracted-dir", type=Path, default=ROOT / "data" / "raw" / "sqf_docs_extracted")
    sqf_docs.add_argument("--chunk-chars", type=int, default=2400)
    sqf_docs.add_argument("--overlap-chars", type=int, default=250)
    sqf_docs.add_argument("--limit", type=int)
    sqf_docs.add_argument("--ocr-empty", action="store_true")
    sqf_docs.add_argument("--ocr-lang", default="kor+eng")
    sqf_docs.add_argument("--ocr-dpi", type=int, default=180)
    sqf_docs.add_argument("--ocr-max-pages", type=int)
    sqf_docs.add_argument("--only-unprocessed", action="store_true")

    sqf_precision = subparsers.add_parser(
        "build-sqf-precision-matches",
        help="Build precision evidence matches from SQF PDF chunks to SQF job levels.",
    )
    sqf_precision.add_argument("--min-score", type=float, default=9.0)
    sqf_precision.add_argument("--max-matches-per-chunk", type=int, default=8)
    sqf_precision.add_argument("--limit-chunks", type=int)
    sqf_precision.add_argument("--asset-id", type=int)
    sqf_precision.add_argument("--no-reset", action="store_true")

    evaluate = subparsers.add_parser(
        "evaluate",
        help="Write evaluation metrics for NCS-SQF mapping/recommendation quality.",
    )
    evaluate.add_argument("--scope-tag")
    evaluate.add_argument("--run-name", default="mvp")

    refine = subparsers.add_parser(
        "refine",
        help="Generate/apply LLM-ready refinement jobs from quality issues.",
    )
    refine.add_argument("action", choices=["generate", "apply", "stats", "export-jsonl", "import-jsonl"])
    refine.add_argument("--issue-types", help="Comma separated issue types.")
    refine.add_argument("--target-types", help="Comma separated target types.")
    refine.add_argument("--severity")
    refine.add_argument("--provider", default="local-rule")
    refine.add_argument("--limit", type=int, default=50)
    refine.add_argument("--min-confidence", type=float, default=0.95)
    refine.add_argument("--dry-run", action="store_true")
    refine.add_argument("--out", type=Path)
    refine.add_argument("--input", type=Path)

    lint = subparsers.add_parser("lint", help="Check docs, boundaries, secrets, and DB invariants.")
    lint.add_argument("--strict", action="store_true")

    pipeline = subparsers.add_parser("pipeline", help="Run selected pipeline stages.")
    pipeline.add_argument("--preprocess", action="store_true")
    pipeline.add_argument("--reset", action="store_true")
    pipeline.add_argument("--allow-append", action="store_true")
    pipeline.add_argument("--sheets")
    pipeline.add_argument("--max-rows", type=int)
    pipeline.add_argument("--quality", action="store_true")
    pipeline.add_argument("--api-standards", action="store_true")
    pipeline.add_argument("--api-subd", action="store_true")
    pipeline.add_argument("--api-elements-hr", action="store_true")
    pipeline.add_argument("--api-sqf", action="store_true")
    pipeline.add_argument("--sqf-major-code")
    pipeline.add_argument("--sqf-major-limit", type=int)
    pipeline.add_argument("--collect-sqf-library", action="store_true")
    pipeline.add_argument("--download-sqf-library", action="store_true")
    pipeline.add_argument("--overwrite-sqf-library", action="store_true")
    pipeline.add_argument("--sqf-library-start-page", type=int, default=0)
    pipeline.add_argument("--sqf-library-end-page", type=int, default=10)
    pipeline.add_argument("--sqf-library-delay", type=float, default=0.2)
    pipeline.add_argument("--build-sqf-sqlite-model", action="store_true")
    pipeline.add_argument("--preprocess-sqf-documents", action="store_true")
    pipeline.add_argument("--sqf-chunk-chars", type=int, default=2400)
    pipeline.add_argument("--sqf-overlap-chars", type=int, default=250)
    pipeline.add_argument("--sqf-ocr-empty", action="store_true")
    pipeline.add_argument("--sqf-ocr-lang", default="kor+eng")
    pipeline.add_argument("--sqf-ocr-dpi", type=int, default=180)
    pipeline.add_argument("--sqf-ocr-max-pages", type=int)
    pipeline.add_argument("--sqf-only-unprocessed", action="store_true")
    pipeline.add_argument("--build-sqf-precision-matches", action="store_true")
    pipeline.add_argument("--sqf-precision-min-score", type=float, default=9.0)
    pipeline.add_argument("--sqf-precision-max-matches-per-chunk", type=int, default=8)
    pipeline.add_argument("--sqf-precision-asset-id", type=int)
    pipeline.add_argument("--build-sqf-mappings", action="store_true")
    pipeline.add_argument("--all-sqf-mappings", action="store_true")
    pipeline.add_argument("--mapping-major-code")
    pipeline.add_argument("--mapping-keyword")
    pipeline.add_argument("--mapping-source-key")
    pipeline.add_argument("--mapping-limit-per-duty", type=int, default=10)
    pipeline.add_argument("--mapping-duty-limit", type=int, default=5000)
    pipeline.add_argument("--refine", action="store_true")
    pipeline.add_argument("--refine-issue-types")
    pipeline.add_argument("--refine-target-types")
    pipeline.add_argument("--refine-limit", type=int, default=50)
    pipeline.add_argument("--apply-refinements", action="store_true")
    pipeline.add_argument("--smoke", action="store_true")
    pipeline.add_argument("--lint", action="store_true")
    pipeline.add_argument("--strict-lint", action="store_true")
    pipeline.add_argument("--timeout", type=int, default=90)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "inspect":
        print_json(inspect_project())
    elif args.command == "smoke":
        print_json(
            run_smoke_check(
                major_code=args.major_code,
                middle_code=args.middle_code,
                small_code=args.small_code,
                sub_code=args.sub_code,
            )
        )
    elif args.command == "plan-elements":
        print_json(plan_element_batches(args.batch_size, args.concurrency))
    elif args.command == "dashboard":
        print_json(
            {
                "command": f"python scripts\\ncs_dashboard.py --host {args.host} --port {args.port}",
                "url": f"http://{args.host}:{args.port}",
            }
        )
    elif args.command == "build-sqf-mappings":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            print_json(
                build_sqf_mapping_candidates(
                    conn,
                    mvp_only=not args.all_sqf,
                    major_code=args.major_code,
                    keyword=args.keyword,
                    source_key=args.source_key,
                    limit_per_duty=args.limit_per_duty,
                    duty_limit=args.duty_limit,
                )
            )
        finally:
            conn.close()
    elif args.command == "export-package":
        settings = load_settings()
        print_json(
            export_handoff_package(
                settings.db_path,
                Path(args.out),
                db_mode=args.db_mode,
                zip_output=args.zip,
            )
        )
    elif args.command == "collect-sqf-library":
        settings = load_settings()
        print_json(
            collect_sqf_library(
                settings.db_path,
                raw_dir=args.raw_dir,
                start_page=args.start_page,
                end_page=args.end_page,
                download=args.download,
                timeout=args.timeout,
                overwrite=args.overwrite,
                delay=args.delay,
            )
        )
    elif args.command == "build-sqf-sqlite-model":
        settings = load_settings()
        print_json(sqf_model_summary(settings.db_path) if args.summary else build_sqf_sqlite_model(settings.db_path))
    elif args.command == "preprocess-sqf-documents":
        settings = load_settings()
        print_json(
            preprocess_sqf_documents(
                settings.db_path,
                extracted_dir=args.extracted_dir,
                chunk_chars=args.chunk_chars,
                overlap_chars=args.overlap_chars,
                limit=args.limit,
                ocr_empty=args.ocr_empty,
                ocr_lang=args.ocr_lang,
                ocr_dpi=args.ocr_dpi,
                ocr_max_pages=args.ocr_max_pages,
                only_unprocessed=args.only_unprocessed,
            )
        )
    elif args.command == "build-sqf-precision-matches":
        settings = load_settings()
        print_json(
            build_sqf_chunk_job_level_matches(
                settings.db_path,
                min_score=args.min_score,
                max_matches_per_chunk=args.max_matches_per_chunk,
                limit_chunks=args.limit_chunks,
                asset_id=args.asset_id,
                reset=not args.no_reset,
            )
        )
    elif args.command == "evaluate":
        settings = load_settings()
        print_json(run_evaluation(settings.db_path, scope_tag=args.scope_tag, run_name=args.run_name))
    elif args.command == "refine":
        settings = load_settings()
        print_json(
            run_refinement_harness(
                settings.db_path,
                action=args.action,
                issue_types=parse_csv(args.issue_types),
                target_types=parse_csv(args.target_types),
                severity=args.severity,
                provider=args.provider,
                limit=args.limit,
                min_confidence=args.min_confidence,
                dry_run=args.dry_run,
                out_path=args.out,
                input_path=args.input,
            )
        )
    elif args.command == "lint":
        result = lint_repo(strict=args.strict)
        print_json(result)
        if not result["ok"]:
            raise SystemExit(1)
    elif args.command == "pipeline":
        print_json(run_pipeline(args))


if __name__ == "__main__":
    main()
