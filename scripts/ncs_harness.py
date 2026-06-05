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

from ncs_mcp.collect_api import collect_elements_api, collect_standard_api, collect_subd_api
from ncs_mcp.config import load_settings
from ncs_mcp.db import connect, initialize_database
from ncs_mcp.preprocess_excel import preprocess_excel
from ncs_mcp.quality import run_quality_checks
from ncs_mcp.server import get_competency_units, get_unit_structure


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
    "quality_issues",
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

    if settings.service_key:
        for path in scan_text_files():
            if settings.service_key in path.read_text(encoding="utf-8", errors="ignore"):
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
    elif args.command == "lint":
        result = lint_repo(strict=args.strict)
        print_json(result)
        if not result["ok"]:
            raise SystemExit(1)
    elif args.command == "pipeline":
        print_json(run_pipeline(args))


if __name__ == "__main__":
    main()
