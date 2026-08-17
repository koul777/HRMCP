from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS = PROJECT_ROOT / "reports"
DECISION_FIELDS = (
    "decision",
    "rationale",
    "reviewer_id",
    "reviewed_at",
    "source_decision_packet",
    "evidence_refs_json",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def portable_path(path: str | Path, *, root: Path = PROJECT_ROOT) -> str:
    resolved = Path(path).resolve(strict=False)
    try:
        return resolved.relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def dated_artifact_sort_key(path: Path) -> tuple[int, float]:
    for part in reversed(path.stem.split("_")):
        if len(part) == 8 and part.isdigit():
            return int(part), path.stat().st_mtime
    return 0, path.stat().st_mtime


def latest_report_path(*patterns: str, reports_dir: Path = REPORTS) -> Path:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in reports_dir.glob(pattern):
            if path.is_file() and path not in seen:
                seen.add(path)
                candidates.append(path)
    if not candidates:
        raise FileNotFoundError(f"no report artifact matched: {patterns}")
    return max(candidates, key=dated_artifact_sort_key)


def artifact_date_suffix(path: Path | None) -> str:
    if path is None:
        return ""
    match = re.search(r"(_\d{8}(?:_[A-Za-z0-9]+)*)$", path.stem)
    return match.group(1) if match else ""


def input_or_latest(value: Path | None, *patterns: str) -> Path:
    return value if value is not None else latest_report_path(*patterns)


def resolve_reports_artifact(ref: str | None, *, root: Path = PROJECT_ROOT) -> Path | None:
    artifact = str(ref or "").strip().partition("#")[0].strip()
    if not artifact:
        return None
    path = Path(artifact)
    candidate = path if path.is_absolute() else root / path
    try:
        resolved = candidate.resolve(strict=True)
        reports_root = (root / "reports").resolve(strict=True)
        resolved.relative_to(reports_root)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def is_false_cell(value: Any) -> bool:
    if value is False:
        return True
    return str(value).strip().lower() == "false"


def split_csv_cell(value: str | None) -> list[str]:
    return [
        part.strip()
        for part in str(value or "").split(",")
        if part.strip()
    ]


def sorted_unique(values: list[str]) -> list[str]:
    return sorted({value for value in values if value})


def _scenario_sort_key(scenario_id: str) -> tuple[int, int | str]:
    return (0, int(scenario_id)) if scenario_id.isdigit() else (1, scenario_id)


def _decision_json_for_csv(path: Path) -> Path | None:
    candidate = path.with_suffix(".json")
    return candidate if candidate.exists() and candidate.is_file() else None


def _source_packet_from_decision_json(
    decision_sheet_json: Path | None,
    *,
    root: Path,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    if not decision_sheet_json or not decision_sheet_json.exists():
        return None, None, None
    payload = read_json(decision_sheet_json)
    source_packet = str(payload.get("source_packet") or "").strip() or None
    source_packet_sha = str(payload.get("source_packet_sha256") or "").strip() or None
    if source_packet:
        source_packet = portable_path(root / source_packet if not Path(source_packet).is_absolute() else source_packet, root=root)
    return source_packet, source_packet_sha, payload


def build_crosswalk(
    *,
    transition_gap_csv: Path,
    provenance_decision_sheet_csv: Path,
    provenance_decision_sheet_json: Path | None = None,
    generated_at: str | None = None,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    generated_at = generated_at or now_iso()
    if provenance_decision_sheet_json is None:
        provenance_decision_sheet_json = _decision_json_for_csv(provenance_decision_sheet_csv)

    transition_rows = read_csv_rows(transition_gap_csv)
    decision_rows = read_csv_rows(provenance_decision_sheet_csv)
    source_packet, source_packet_sha, decision_json = _source_packet_from_decision_json(
        provenance_decision_sheet_json,
        root=root,
    )

    transition_by_scenario: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in transition_rows:
        scenario_id = str(row.get("scenario_id") or "").strip()
        if scenario_id:
            transition_by_scenario[scenario_id].append(row)

    decision_by_scenario = {
        str(row.get("target_id") or "").strip(): row
        for row in decision_rows
        if row.get("surface") == "training_transition_gold_scenarios"
        and str(row.get("target_id") or "").strip()
    }

    source_packet_exists = False
    if source_packet:
        source_packet_exists = resolve_reports_artifact(source_packet, root=root) is not None

    rows: list[dict[str, Any]] = []
    for scenario_id in sorted(transition_by_scenario, key=_scenario_sort_key):
        gap_rows = transition_by_scenario[scenario_id]
        first = gap_rows[0]
        decision_row = decision_by_scenario.get(scenario_id)
        order = str(decision_row.get("order") or "").strip() if decision_row else ""
        available_packet_ref = f"{source_packet}#order:{order}" if source_packet and order else source_packet
        gap_recommended_ref = str(first.get("recommended_source_decision_packet") or "").strip()
        gap_recommended_path = resolve_reports_artifact(gap_recommended_ref, root=root)
        operator_packet_available = bool(source_packet_exists and available_packet_ref)
        if operator_packet_available:
            recommended_packet_ref = available_packet_ref or ""
            recommended_packet_exists = True
            recommended_packet_source = "operator_reconfirmation_packet"
        elif gap_recommended_path is not None:
            recommended_packet_ref = gap_recommended_ref
            recommended_packet_exists = True
            recommended_packet_source = "gap_recommended_packet"
        else:
            recommended_packet_ref = ""
            recommended_packet_exists = False
            recommended_packet_source = "missing"
        audit_ids = sorted_unique(
            [str(row.get("audit_id") or "").strip() for row in gap_rows]
        )
        gap_fields = sorted_unique(
            [
                field
                for row in gap_rows
                for field in split_csv_cell(row.get("gap_fields"))
            ]
        )
        required_evidence_refs = str(first.get("required_evidence_refs_json") or "").strip()
        rows.append(
            {
                "scenario_id": scenario_id,
                "scenario_name": str(first.get("scenario_name") or "").strip(),
                "current_query": str(first.get("current_query") or "").strip(),
                "target_query": str(first.get("target_query") or "").strip(),
                "expected_course_names_json": str(first.get("expected_course_names_json") or "").strip(),
                "transition_gap_row_count": len(gap_rows),
                "audit_ids": audit_ids,
                "gap_fields": gap_fields,
                "required_action": str(first.get("required_action") or "").strip(),
                "required_evidence_refs_json": required_evidence_refs,
                "gap_recommended_source_decision_packet": gap_recommended_ref,
                "gap_recommended_packet_artifact_exists": gap_recommended_path is not None,
                "recommended_source_decision_packet_ref": recommended_packet_ref,
                "recommended_source_decision_packet_artifact_exists": recommended_packet_exists,
                "recommended_source_decision_packet_source": recommended_packet_source,
                "decision_sheet_order": order,
                "decision_sheet_row_found": decision_row is not None,
                "decision_sheet_target": (
                    f"{decision_row.get('target_table')}:{decision_row.get('target_id')}"
                    if decision_row
                    else ""
                ),
                "decision_sheet_display": str(decision_row.get("display") or "").strip()
                if decision_row
                else "",
                "decision_sheet_provenance_state": str(decision_row.get("provenance_state") or "").strip()
                if decision_row
                else "",
                "decision_sheet_row_sha256": str(decision_row.get("source_packet_row_sha256") or "").strip()
                if decision_row
                else "",
                "operator_source_decision_packet_ref": available_packet_ref or "",
                "operator_source_artifact_hash": source_packet_sha or "",
                "source_packet_exists": source_packet_exists,
                "operator_decision_fields_blank": all(
                    not str(decision_row.get(field) or "").strip()
                    for field in DECISION_FIELDS
                )
                if decision_row
                else False,
                "operator_guard_fields_false": all(
                    is_false_cell(decision_row.get(field))
                    for field in ("status_update_allowed", "db_writes", "approval_claim")
                    if field in decision_row
                )
                if decision_row
                else False,
                "operator_instruction": (
                    "If a human reviewer reconfirms this scenario, use the existing "
                    "operator_source_decision_packet_ref and operator_source_artifact_hash "
                    "as packet evidence in the later guarded audit workflow. This crosswalk "
                    "does not update statuses."
                ),
            }
        )

    decision_only_scenarios = sorted(
        set(decision_by_scenario) - set(transition_by_scenario),
        key=_scenario_sort_key,
    )
    source_paths = {
        "transition_gap_csv": portable_path(transition_gap_csv, root=root),
        "provenance_decision_sheet_csv": portable_path(provenance_decision_sheet_csv, root=root),
        "provenance_decision_sheet_json": portable_path(provenance_decision_sheet_json, root=root)
        if provenance_decision_sheet_json
        else None,
        "provenance_source_packet": source_packet,
    }
    source_path_objects = {
        "transition_gap_csv": transition_gap_csv,
        "provenance_decision_sheet_csv": provenance_decision_sheet_csv,
        "provenance_decision_sheet_json": provenance_decision_sheet_json,
        "provenance_source_packet": resolve_reports_artifact(source_packet, root=root)
        if source_packet
        else None,
    }
    source_suffixes = {
        key: artifact_date_suffix(path)
        for key, path in source_path_objects.items()
        if path is not None
    }
    nonblank_suffixes = {suffix for suffix in source_suffixes.values() if suffix}
    report = {
        "ok": True,
        "schema": "transition_provenance_operator_crosswalk_v1",
        "generated_at": generated_at,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "human_decision_required": True,
        "source_paths": source_paths,
        "source_hashes": {
            key: sha256_file(path) if path else None
            for key, path in source_path_objects.items()
        },
        "source_artifact_suffixes": source_suffixes,
        "source_artifact_suffix_family_ok": len(nonblank_suffixes) <= 1,
        "decision_sheet_generated_at": decision_json.get("generated_at") if decision_json else None,
        "decision_sheet_content_sha256_excluding_self_hash": (
            decision_json.get("content_sha256_excluding_self_hash") if decision_json else None
        ),
        "scenario_count": len(transition_by_scenario),
        "crosswalk_row_count": len(rows),
        "decision_only_transition_scenario_ids": decision_only_scenarios,
        "missing_decision_sheet_scenario_ids": sorted(
            set(transition_by_scenario) - set(decision_by_scenario),
            key=_scenario_sort_key,
        ),
        "missing_gap_recommended_packet_artifact_count": sum(
            1 for row in rows if not row["gap_recommended_packet_artifact_exists"]
        ),
        "operator_ready_row_count": sum(
            1
            for row in rows
            if row["decision_sheet_row_found"]
            and row["source_packet_exists"]
            and row["operator_decision_fields_blank"]
            and row["operator_guard_fields_false"]
        ),
        "required_human_fields": [
            "decision",
            "rationale",
            "reviewer_id",
            "reviewed_at",
            "source_decision_packet",
            "evidence_refs_json",
        ],
        "allowed_decisions": [
            "reconfirm",
            "downgrade_to_review_required",
            "defer",
        ],
        "policy": {
            "does_not_change_existing_statuses": True,
            "not_a_guarded_import_plan": True,
            "no_automatic_status_promotion": True,
            "reconfirm_is_evidence_review_only": True,
            "source_packet_hash_must_match_before_future_import": True,
        },
        "rows": rows,
    }
    return report


def audit_crosswalk(report: dict[str, Any], *, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    for field in ("report_only", "human_decision_required"):
        if report.get(field) is not True:
            issues.append({"code": f"{field}_not_true"})
    for field in ("status_update_allowed", "db_writes", "api_calls", "approval_claim"):
        if report.get(field) is not False:
            issues.append({"code": f"{field}_not_false"})

    source_paths = report.get("source_paths") if isinstance(report.get("source_paths"), dict) else {}
    source_hashes = report.get("source_hashes") if isinstance(report.get("source_hashes"), dict) else {}
    source_hash_checks: dict[str, dict[str, Any]] = {}
    for key, source in source_paths.items():
        if not source:
            continue
        resolved = resolve_reports_artifact(str(source), root=root)
        actual_hash = sha256_file(resolved) if resolved else None
        expected_hash = source_hashes.get(key)
        check = {
            "path": source,
            "exists_nonempty": bool(resolved and resolved.stat().st_size > 0),
            "expected_sha256": expected_hash,
            "actual_sha256": actual_hash,
            "matches": bool(expected_hash and actual_hash and expected_hash == actual_hash),
        }
        source_hash_checks[key] = check
        if not check["exists_nonempty"]:
            issues.append({"code": "source_artifact_missing", "source": key, "path": source})
        elif expected_hash != actual_hash:
            issues.append({"code": "source_artifact_hash_mismatch", "source": key, "path": source})

    if report.get("source_artifact_suffix_family_ok") is not True:
        issues.append(
            {
                "code": "source_artifact_suffix_family_mismatch",
                "source_artifact_suffixes": report.get("source_artifact_suffixes"),
            }
        )

    for row in report.get("rows") or []:
        scenario_id = str(row.get("scenario_id") or "")
        if not row.get("decision_sheet_row_found"):
            issues.append({"code": "missing_decision_sheet_row", "scenario_id": scenario_id})
        if not row.get("source_packet_exists"):
            issues.append({"code": "source_packet_missing", "scenario_id": scenario_id})
        if not row.get("operator_decision_fields_blank"):
            issues.append({"code": "operator_decision_fields_not_blank", "scenario_id": scenario_id})
        if not row.get("operator_guard_fields_false"):
            issues.append({"code": "operator_guard_fields_not_false", "scenario_id": scenario_id})
        if not row.get("operator_source_artifact_hash"):
            issues.append({"code": "operator_source_artifact_hash_missing", "scenario_id": scenario_id})
        if not row.get("recommended_source_decision_packet_artifact_exists"):
            warnings.append(
                {
                    "code": "recommended_packet_artifact_missing",
                    "scenario_id": scenario_id,
                    "gap_recommended_path": row.get("gap_recommended_source_decision_packet"),
                    "operator_source_path": row.get("operator_source_decision_packet_ref"),
                }
            )
        elif row.get("gap_recommended_source_decision_packet") and not row.get(
            "gap_recommended_packet_artifact_exists"
        ):
            diagnostics.append(
                {
                    "code": "legacy_gap_recommended_packet_artifact_missing",
                    "scenario_id": scenario_id,
                    "non_blocking": True,
                    "reason": (
                        "The primary recommended_source_decision_packet_ref resolves to the "
                        "operator reconfirmation packet; the legacy gap packet path is "
                        "diagnostic only."
                    ),
                    "primary_recommended_source_decision_packet_ref": row.get(
                        "recommended_source_decision_packet_ref"
                    ),
                    "primary_recommended_source_decision_packet_source": row.get(
                        "recommended_source_decision_packet_source"
                    ),
                    "gap_recommended_path": row.get("gap_recommended_source_decision_packet"),
                }
            )

    audit = {
        "schema": "transition_provenance_operator_crosswalk_audit_v1",
        "generated_at": now_iso(),
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "ok": not issues,
        "issue_count": len(issues),
        "warning_count": len(warnings),
        "diagnostic_count": len(diagnostics),
        "issues": issues,
        "warnings": warnings,
        "diagnostics": diagnostics,
        "source_hash_checks": source_hash_checks,
        "crosswalk_row_count": report.get("crosswalk_row_count"),
        "operator_ready_row_count": report.get("operator_ready_row_count"),
        "missing_gap_recommended_packet_artifact_count": report.get(
            "missing_gap_recommended_packet_artifact_count"
        ),
        "legacy_gap_recommended_packet_diagnostic_count": len(diagnostics),
        "legacy_gap_recommended_packet_missing_is_non_blocking_when_primary_exists": True,
    }
    return audit


CSV_FIELDS = [
    "scenario_id",
    "scenario_name",
    "current_query",
    "target_query",
    "transition_gap_row_count",
    "audit_ids",
    "gap_fields",
    "decision_sheet_order",
    "decision_sheet_row_found",
    "recommended_source_decision_packet_ref",
    "recommended_source_decision_packet_artifact_exists",
    "recommended_source_decision_packet_source",
    "operator_source_decision_packet_ref",
    "operator_source_artifact_hash",
    "required_evidence_refs_json",
    "gap_recommended_source_decision_packet",
    "gap_recommended_packet_artifact_exists",
    "operator_decision_fields_blank",
    "operator_guard_fields_false",
]


def csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def write_csv(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in report.get("rows") or []:
            writer.writerow({field: csv_cell(row.get(field)) for field in CSV_FIELDS})


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Transition Provenance Operator Crosswalk",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- report_only: `{report.get('report_only')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- api_calls: `{report.get('api_calls')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        f"- scenario_count: `{report.get('scenario_count')}`",
        f"- operator_ready_row_count: `{report.get('operator_ready_row_count')}`",
        (
            "- missing_gap_recommended_packet_artifact_count: "
            f"`{report.get('missing_gap_recommended_packet_artifact_count')}` "
            "(legacy diagnostic; non-blocking when primary recommended packet exists)"
        ),
        "",
        "This crosswalk is review-only. It does not set `human_reviewed`, `accepted`, or `reviewed` and does not perform DB writes.",
        "Use `recommended_source_decision_packet_ref` as the primary packet evidence. Legacy gap packet paths are retained only as diagnostics.",
        "",
        "## Source Paths",
    ]
    for key, value in (report.get("source_paths") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(
        [
            "",
            "## Rows",
            "",
            "| scenario | decision row | primary recommended packet ref | primary source | operator source packet ref | source hash | legacy gap packet exists (non-blocking if primary exists) |",
            "|---:|---:|---|---|---|---|---|",
        ]
    )
    for row in report.get("rows") or []:
        lines.append(
            "| "
            f"{row.get('scenario_id')} | "
            f"{row.get('decision_sheet_order')} | "
            f"`{row.get('recommended_source_decision_packet_ref')}` | "
            f"`{row.get('recommended_source_decision_packet_source')}` | "
            f"`{row.get('operator_source_decision_packet_ref')}` | "
            f"`{row.get('operator_source_artifact_hash')}` | "
            f"`{row.get('gap_recommended_packet_artifact_exists')}` |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_audit_markdown(path: Path, audit: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Transition Provenance Operator Crosswalk Audit",
        "",
        f"- schema: `{audit.get('schema')}`",
        f"- generated_at: `{audit.get('generated_at')}`",
        f"- ok: `{audit.get('ok')}`",
        f"- issue_count: `{audit.get('issue_count')}`",
        f"- warning_count: `{audit.get('warning_count')}`",
        f"- diagnostic_count: `{audit.get('diagnostic_count')}`",
        f"- report_only: `{audit.get('report_only')}`",
        f"- status_update_allowed: `{audit.get('status_update_allowed')}`",
        f"- db_writes: `{audit.get('db_writes')}`",
        f"- api_calls: `{audit.get('api_calls')}`",
        f"- approval_claim: `{audit.get('approval_claim')}`",
        "",
    ]
    if audit.get("issues"):
        lines.append("## Issues")
        for issue in audit.get("issues") or []:
            lines.append(f"- `{issue.get('code')}`: `{issue}`")
    else:
        lines.append("No crosswalk integrity issues found.")
    if audit.get("warnings"):
        lines.extend(["", "## Warnings"])
        for warning in audit.get("warnings") or []:
            lines.append(f"- `{warning.get('code')}`: `{warning}`")
    if audit.get("diagnostics"):
        lines.extend(["", "## Diagnostics"])
        for diagnostic in audit.get("diagnostics") or []:
            lines.append(f"- `{diagnostic.get('code')}`: `{diagnostic}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a report-only transition scenario to provenance decision crosswalk."
    )
    parser.add_argument("--transition-gap-csv", type=Path)
    parser.add_argument("--provenance-decision-sheet-csv", type=Path)
    parser.add_argument("--provenance-decision-sheet-json", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--csv-out", type=Path)
    parser.add_argument("--audit-out", type=Path)
    parser.add_argument("--audit-markdown-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    transition_gap_csv = input_or_latest(
        args.transition_gap_csv,
        "transition_trusted_scenario_provenance_gap_*.csv",
    )
    decision_sheet_csv = input_or_latest(
        args.provenance_decision_sheet_csv,
        "human_review_provenance_reconfirmation_decision_sheet_*.csv",
    )
    report = build_crosswalk(
        transition_gap_csv=transition_gap_csv,
        provenance_decision_sheet_csv=decision_sheet_csv,
        provenance_decision_sheet_json=args.provenance_decision_sheet_json,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out:
        write_markdown(args.markdown_out, report)
    if args.csv_out:
        write_csv(args.csv_out, report)

    audit = None
    if args.audit_out or args.audit_markdown_out or args.strict:
        audit = audit_crosswalk(report)
        if args.audit_out:
            args.audit_out.parent.mkdir(parents=True, exist_ok=True)
            args.audit_out.write_text(
                json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if args.audit_markdown_out:
            write_audit_markdown(args.audit_markdown_out, audit)

    print(
        json.dumps(
            {
                "ok": report.get("ok"),
                "schema": report.get("schema"),
                "scenario_count": report.get("scenario_count"),
                "operator_ready_row_count": report.get("operator_ready_row_count"),
                "out": str(args.out),
                "audit_ok": audit.get("ok") if audit else None,
                "audit_issue_count": audit.get("issue_count") if audit else None,
                "audit_warning_count": audit.get("warning_count") if audit else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.strict and audit is not None and not audit.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
