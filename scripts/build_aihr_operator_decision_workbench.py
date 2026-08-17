from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "aihr_operator_decision_workbench_v1"
FORBIDDEN_AUTOMATIC_STATUSES = ["human_reviewed", "accepted", "reviewed"]
DECISION_FIELDS = (
    "decision",
    "rationale",
    "reviewer_id",
    "reviewed_at",
    "source_decision_packet",
    "evidence_refs_json",
)
GUARD_FIELDS = ("status_update_allowed", "db_writes", "approval_claim")
PREVIEW_FIELDS = (
    "scenario_id",
    "decision_sheet_order",
    "order",
    "sequence",
    "wave",
    "issue_type",
    "target_type",
    "target_id",
    "display",
    "status_trust",
    "provenance_state",
    "requested_decision_options",
    "recommended_source_decision_packet_ref",
    "operator_source_decision_packet_ref",
    "source_context_excerpt",
    "issue_detail",
    "purpose",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_fragment(value: str | Path | None) -> str:
    return str(value or "").partition("#")[0].strip()


def resolve_artifact(value: str | Path | None, *, root: Path = PROJECT_ROOT) -> Path | None:
    text = strip_fragment(value)
    if not text:
        return None
    path = Path(text)
    if path.is_absolute():
        return path
    rooted = root / path
    if rooted.exists():
        return rooted
    if path.exists():
        return path
    return rooted


def portable_path(path: str | Path | None, *, root: Path = PROJECT_ROOT) -> str | None:
    text = strip_fragment(path)
    if not text:
        return None
    resolved = Path(text).resolve(strict=False)
    try:
        return resolved.relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def source_hash_checks(
    source_paths: dict[str, str | None],
    source_hashes: dict[str, str | None],
    *,
    root: Path,
) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    for key, value in source_paths.items():
        if not value:
            continue
        resolved = resolve_artifact(value, root=root)
        actual = sha256_file(resolved)
        expected = source_hashes.get(key)
        checks[key] = {
            "path": portable_path(value, root=root),
            "expected_sha256": expected,
            "actual_sha256": actual,
            "hash_matches": bool(expected and actual and expected == actual),
        }
    return checks


def read_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def read_csv(path: Path | None) -> tuple[list[str], list[dict[str, str]]]:
    if not path or not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows: list[dict[str, str]] = []
        for line_number, row in enumerate(reader, start=2):
            item = dict(row)
            item["__source_row_number"] = str(line_number)
            rows.append(item)
        return list(reader.fieldnames or []), rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "rank",
        "sprint_id",
        "blocker",
        "source_path",
        "source_row_key",
        "source_row_number",
        "selected_order",
        "scope_match_ok",
        "crosswalk_contract_ok",
        "row_preview",
        "decision_options",
        "required_human_fields",
        "decision_fields_blank_ok",
        "guard_fields_false_ok",
        "operator_instruction",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def safe_contract(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        "report_only_is_true": payload.get("report_only") is True,
        "status_update_allowed_is_false": payload.get("status_update_allowed") is False,
        "db_writes_is_false": payload.get("db_writes") is False,
        "api_calls_is_false_or_absent": payload.get("api_calls") in (False, None),
        "approval_claim_is_false": payload.get("approval_claim") is False,
        "acceptance_claim_is_false_or_absent": payload.get("acceptance_claim") in (False, None),
        "human_decision_required_is_true": payload.get("human_decision_required") is True,
    }


def is_false(value: Any) -> bool:
    return value is False or str(value).strip().lower() == "false"


def row_key(row: dict[str, str]) -> str:
    for field in ("scenario_id", "order", "sequence", "wave", "target_id"):
        value = str(row.get(field) or "").strip()
        if value:
            return value
    return ""


def preview(row: dict[str, str], *, limit: int = 500) -> str:
    parts = []
    for field in PREVIEW_FIELDS:
        value = str(row.get(field) or "").strip()
        if value:
            parts.append(f"{field}={value}")
    text = " | ".join(parts)
    return text[:limit]


def source_for_entry(entry: dict[str, Any]) -> str | None:
    return str(entry.get("open_first") or "").strip() or None


def issue_type_filter(row_selector: str) -> str | None:
    marker = "issue_type="
    if marker not in row_selector:
        return None
    return row_selector.split(marker, 1)[1].split(";", 1)[0].strip()


def surface_order_ranges(row_selector: str) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    for match in re.finditer(
        r"rows\s+(\d+)\s*-\s*(\d+)\s+([A-Za-z0-9_]+)",
        row_selector,
        flags=re.IGNORECASE,
    ):
        start = int(match.group(1))
        end = int(match.group(2))
        if start > end:
            start, end = end, start
        ranges.append({"start": start, "end": end, "surface": match.group(3)})
    return ranges


def row_order_int(row: dict[str, str]) -> int | None:
    value = str(row.get("order") or "").strip()
    return int(value) if value.isdigit() else None


def sort_rows_by_first_ids(
    rows: list[dict[str, str]],
    first_ids: list[str],
) -> list[dict[str, str]]:
    if not first_ids:
        return rows
    order = {str(value): index for index, value in enumerate(first_ids)}
    return sorted(
        rows,
        key=lambda row: (
            order.get(row_key(row), len(order)),
            int(row.get("order") or row.get("sequence") or "999999")
            if str(row.get("order") or row.get("sequence") or "").isdigit()
            else 999999,
        ),
    )


def selected_rows(
    entry: dict[str, Any],
    rows: list[dict[str, str]],
    *,
    per_sprint_limit: int,
) -> list[dict[str, str]]:
    selector = str(entry.get("row_selector") or "")
    filtered = list(rows)
    issue_filter = issue_type_filter(selector)
    if issue_filter:
        filtered = [row for row in filtered if str(row.get("issue_type") or "") == issue_filter]
    ranges = surface_order_ranges(selector)
    if ranges:
        range_order = {item["surface"]: index for index, item in enumerate(ranges)}

        def in_declared_range(row: dict[str, str]) -> bool:
            surface = str(row.get("surface") or "")
            order = row_order_int(row)
            return any(
                surface == item["surface"]
                and order is not None
                and item["start"] <= order <= item["end"]
                for item in ranges
            )

        filtered = [row for row in filtered if in_declared_range(row)]
        filtered = sorted(
            filtered,
            key=lambda row: (
                range_order.get(str(row.get("surface") or ""), len(ranges)),
                row_order_int(row) or 999999,
            ),
        )
    else:
        filtered = sort_rows_by_first_ids(
            filtered,
            [str(value) for value in entry.get("first_row_ids") or []],
        )
    return filtered[: max(0, per_sprint_limit)]


def decision_blank_ok(row: dict[str, str], required_fields: list[str]) -> bool | None:
    fields = [field for field in required_fields if field in row and field in DECISION_FIELDS]
    if not fields:
        return None
    return all(not str(row.get(field) or "").strip() for field in fields)


def guard_false_ok(row: dict[str, str]) -> bool | None:
    fields = [field for field in GUARD_FIELDS if field in row]
    if not fields:
        return None
    return all(is_false(row.get(field)) for field in fields)


def crosswalk_contract(row: dict[str, str]) -> dict[str, bool]:
    if not any(field in row for field in ("operator_source_decision_packet_ref", "operator_source_artifact_hash")):
        return {}
    contract = {
        "operator_source_decision_packet_ref_present": bool(
            str(row.get("operator_source_decision_packet_ref") or "").strip()
        ),
        "operator_source_artifact_hash_present": str(
            row.get("operator_source_artifact_hash") or ""
        ).strip().startswith("sha256:"),
        "operator_decision_fields_blank_is_true": str(
            row.get("operator_decision_fields_blank") or ""
        ).strip().lower()
        == "true",
        "operator_guard_fields_false_is_true": str(
            row.get("operator_guard_fields_false") or ""
        ).strip().lower()
        == "true",
    }
    if "decision_sheet_row_found" in row:
        contract["decision_sheet_row_found_is_true"] = str(
            row.get("decision_sheet_row_found") or ""
        ).strip().lower() == "true"
    if "recommended_source_decision_packet_artifact_exists" in row:
        contract["recommended_packet_artifact_exists_is_true"] = str(
            row.get("recommended_source_decision_packet_artifact_exists") or ""
        ).strip().lower() == "true"
    return contract


def expected_first_ids(entry: dict[str, Any], *, per_sprint_limit: int) -> list[str]:
    ids = [str(value) for value in entry.get("first_row_ids") or [] if str(value).strip()]
    return ids[: max(0, per_sprint_limit)]


def declared_row_count(entry: dict[str, Any]) -> int | None:
    value = entry.get("row_count")
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def add_issue(issues: list[dict[str, Any]], code: str, message: str, **extra: Any) -> None:
    issues.append({"code": code, "message": message, **extra})


def build_workbench(
    *,
    sprint_queue_path: Path,
    entrypoint_manifest_path: Path | None = None,
    root: Path = PROJECT_ROOT,
    per_sprint_limit: int = 10,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or now_iso()
    sprint_queue = read_json(sprint_queue_path)
    entrypoint_manifest = read_json(entrypoint_manifest_path) if entrypoint_manifest_path else {}
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    source_contracts = {
        "sprint_queue": safe_contract(sprint_queue),
    }
    if entrypoint_manifest:
        source_contracts["entrypoint_manifest"] = safe_contract(entrypoint_manifest)
    for label, contract in source_contracts.items():
        if not all(contract.values()):
            add_issue(
                issues,
                "unsafe_source_contract",
                "Source artifact does not preserve report-only human-gated contract.",
                source=label,
                contract=contract,
            )

    entries = [item for item in sprint_queue.get("queue") or [] if isinstance(item, dict)]
    sprint_records: list[dict[str, Any]] = []
    workbench_rows: list[dict[str, Any]] = []
    for entry in entries:
        source = source_for_entry(entry)
        source_path = resolve_artifact(source, root=root)
        fieldnames, rows = read_csv(source_path)
        if not source_path or not source_path.exists():
            add_issue(
                issues,
                "source_csv_missing",
                "Sprint open_first CSV is missing.",
                sprint_id=entry.get("sprint_id"),
                path=source,
            )
        required_fields = [str(field) for field in entry.get("required_human_fields") or []]
        selected = selected_rows(entry, rows, per_sprint_limit=per_sprint_limit)
        selected_keys = {row_key(row) for row in selected if row_key(row)}
        expected_ids = expected_first_ids(entry, per_sprint_limit=per_sprint_limit)
        missing_expected_ids = [value for value in expected_ids if value not in selected_keys]
        declared_count = declared_row_count(entry)
        minimum_expected_count = (
            min(declared_count, max(0, per_sprint_limit)) if declared_count is not None else None
        )
        scope_match_ok = not missing_expected_ids and (
            minimum_expected_count is None or len(selected) >= minimum_expected_count
        )
        if missing_expected_ids:
            add_issue(
                issues,
                "declared_first_rows_missing",
                "Selected workbench rows do not include the declared first row ids within the sprint limit.",
                sprint_id=entry.get("sprint_id"),
                missing_first_row_ids=missing_expected_ids,
            )
        if minimum_expected_count is not None and len(selected) < minimum_expected_count:
            add_issue(
                issues,
                "declared_row_count_not_satisfied",
                "Selected workbench rows are fewer than the declared row count within the sprint limit.",
                sprint_id=entry.get("sprint_id"),
                declared_row_count=declared_count,
                minimum_expected_count=minimum_expected_count,
                selected_row_count=len(selected),
            )
        if not selected and rows:
            warnings.append(
                {
                    "code": "no_rows_selected_for_sprint",
                    "sprint_id": entry.get("sprint_id"),
                    "row_selector": entry.get("row_selector"),
                }
            )
        row_records: list[dict[str, Any]] = []
        for selected_order, row in enumerate(selected, start=1):
            blank_ok = decision_blank_ok(row, required_fields)
            guard_ok = guard_false_ok(row)
            crosswalk = crosswalk_contract(row)
            crosswalk_ok = all(crosswalk.values()) if crosswalk else None
            if blank_ok is False:
                add_issue(
                    issues,
                    "decision_fields_not_blank",
                    "Selected workbench row has prefilled human decision fields.",
                    sprint_id=entry.get("sprint_id"),
                    source_row_key=row_key(row),
                )
            if guard_ok is False:
                add_issue(
                    issues,
                    "guard_fields_not_false",
                    "Selected workbench row has unsafe guard fields.",
                    sprint_id=entry.get("sprint_id"),
                    source_row_key=row_key(row),
                )
            if crosswalk_ok is False:
                add_issue(
                    issues,
                    "crosswalk_contract_not_safe",
                    "Transition crosswalk row is missing packet mapping evidence or safe operator guard flags.",
                    sprint_id=entry.get("sprint_id"),
                    source_row_key=row_key(row),
                    crosswalk_contract=crosswalk,
                )
            record = {
                "source_row_key": row_key(row),
                "source_row_number": row.get("__source_row_number"),
                "selected_order": selected_order,
                "row_preview": preview(row),
                "scope_match_ok": scope_match_ok,
                "decision_fields_blank_ok": blank_ok,
                "guard_fields_false_ok": guard_ok,
                "crosswalk_contract_ok": crosswalk_ok,
                "crosswalk_contract": crosswalk,
                "selected_fields": {field: row.get(field) for field in PREVIEW_FIELDS if row.get(field)},
            }
            row_records.append(record)
            workbench_rows.append(
                {
                    "rank": entry.get("rank"),
                    "sprint_id": entry.get("sprint_id"),
                    "blocker": entry.get("blocker"),
                    "source_path": portable_path(source_path, root=root),
                    "source_row_key": record["source_row_key"],
                    "source_row_number": record["source_row_number"],
                    "selected_order": selected_order,
                    "scope_match_ok": scope_match_ok,
                    "crosswalk_contract_ok": crosswalk_ok,
                    "row_preview": record["row_preview"],
                    "decision_options": " | ".join(str(value) for value in entry.get("decision_options") or []),
                    "required_human_fields": " | ".join(required_fields),
                    "decision_fields_blank_ok": blank_ok,
                    "guard_fields_false_ok": guard_ok,
                    "operator_instruction": "Fill human fields only after review; this workbench does not authorize apply.",
                }
            )
        source_total_count = declared_count if declared_count is not None else len(row_records)
        unselected_source_count = max(source_total_count - len(row_records), 0)
        if unselected_source_count > 0:
            warnings.append(
                {
                    "code": "selected_workbench_subset",
                    "message": (
                        "Workbench rows are a selected first-pass slice; source rows remain "
                        "for later operator review."
                    ),
                    "sprint_id": entry.get("sprint_id"),
                    "selected_row_count": len(row_records),
                    "source_total_row_count": source_total_count,
                    "unselected_source_row_count": unselected_source_count,
                    "per_sprint_limit": per_sprint_limit,
                }
            )
        sprint_records.append(
            {
                "rank": entry.get("rank"),
                "sprint_id": entry.get("sprint_id"),
                "blocker": entry.get("blocker"),
                "next_safe_action": entry.get("next_safe_action"),
                "open_first": source,
                "source_path": portable_path(source_path, root=root),
                "source_sha256": sha256_file(source_path),
                "source_fieldnames": fieldnames,
                "row_selector": entry.get("row_selector"),
                "declared_row_count": entry.get("row_count"),
                "selected_row_count": len(row_records),
                "source_total_row_count": source_total_count,
                "unselected_source_row_count": unselected_source_count,
                "selected_subset": unselected_source_count > 0,
                "minimum_expected_row_count": minimum_expected_count,
                "expected_first_row_ids": expected_ids,
                "missing_expected_first_row_ids": missing_expected_ids,
                "scope_match_ok": scope_match_ok,
                "required_human_fields": required_fields,
                "decision_options": entry.get("decision_options") or [],
                "forbidden": entry.get("forbidden") or [],
                "rows": row_records,
            }
        )

    source_paths = {
        "sprint_queue": portable_path(sprint_queue_path, root=root),
        "entrypoint_manifest": portable_path(entrypoint_manifest_path, root=root)
        if entrypoint_manifest_path
        else None,
    }
    source_hashes = {
        "sprint_queue": sha256_file(sprint_queue_path),
        "entrypoint_manifest": sha256_file(entrypoint_manifest_path)
        if entrypoint_manifest_path
        else None,
    }
    selected_subset_sprints = [
        sprint for sprint in sprint_records if sprint.get("selected_subset")
    ]

    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "ok": not issues,
        "status": "pass" if not issues else "review_required",
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "acceptance_claim": False,
        "human_decision_required": True,
        "forbidden_automatic_statuses": FORBIDDEN_AUTOMATIC_STATUSES,
        "source_paths": source_paths,
        "source_hashes": source_hashes,
        "source_hash_check_scope": {
            "source_hash_checks": "current_generation_snapshot_only",
            "lineage_validation": False,
        },
        "source_hash_checks": source_hash_checks(source_paths, source_hashes, root=root),
        "source_contracts": source_contracts,
        "summary": {
            "sprint_count": len(sprint_records),
            "workbench_row_count": len(workbench_rows),
            "per_sprint_limit": per_sprint_limit,
            "selected_row_count": len(workbench_rows),
            "source_total_row_count": sum(
                int(sprint.get("source_total_row_count") or 0)
                for sprint in sprint_records
            ),
            "unselected_source_row_count": sum(
                int(sprint.get("unselected_source_row_count") or 0)
                for sprint in sprint_records
            ),
            "selected_subset_sprint_count": len(selected_subset_sprints),
            "decision_rows_with_blank_check_count": sum(
                1 for row in workbench_rows if row.get("decision_fields_blank_ok") is not None
            ),
            "guard_rows_with_false_check_count": sum(
                1 for row in workbench_rows if row.get("guard_fields_false_ok") is not None
            ),
            "issue_count": len(issues),
            "warning_count": len(warnings),
        },
        "sprints": sprint_records,
        "workbench_rows": workbench_rows,
        "issues": issues,
        "warnings": warnings,
        "notes": [
            "This workbench is a consolidated operator reading surface only.",
            "It does not authorize DB writes, API calls, approval, acceptance, or status updates.",
            "Human review fields remain blank until an operator fills the source decision surfaces.",
        ],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "# AI-HR Operator Decision Workbench",
        "",
        f"- schema: `{payload.get('schema')}`",
        f"- generated_at: `{payload.get('generated_at')}`",
        f"- status: `{payload.get('status')}`",
        f"- ok: `{payload.get('ok')}`",
        f"- report_only: `{payload.get('report_only')}`",
        f"- status_update_allowed: `{payload.get('status_update_allowed')}`",
        f"- db_writes: `{payload.get('db_writes')}`",
        f"- api_calls: `{payload.get('api_calls')}`",
        f"- approval_claim: `{payload.get('approval_claim')}`",
        f"- acceptance_claim: `{payload.get('acceptance_claim')}`",
        f"- human_decision_required: `{payload.get('human_decision_required')}`",
        "",
        "## Summary",
        "",
    ]
    for key in (
        "sprint_count",
        "workbench_row_count",
        "per_sprint_limit",
        "selected_row_count",
        "source_total_row_count",
        "unselected_source_row_count",
        "selected_subset_sprint_count",
        "decision_rows_with_blank_check_count",
        "guard_rows_with_false_check_count",
        "issue_count",
        "warning_count",
    ):
        lines.append(f"- {key}: `{summary.get(key)}`")
    if int(summary.get("unselected_source_row_count") or 0) > 0:
        lines.extend(
            [
                "",
                "## Subset Notice",
                "",
                (
                    "- This workbench is a selected first-pass slice; "
                    f"`{summary.get('unselected_source_row_count')}` source rows remain "
                    "outside the current visible batch."
                ),
            ]
        )
    lines.extend(["", "## Sprints", ""])
    for sprint in payload.get("sprints") or []:
        lines.append(
            f"### {sprint.get('rank')}. {sprint.get('sprint_id')} "
            f"rows=`{sprint.get('selected_row_count')}`"
        )
        lines.append(f"- blocker: `{sprint.get('blocker')}`")
        lines.append(f"- next_safe_action: `{sprint.get('next_safe_action')}`")
        lines.append(f"- open_first: `{sprint.get('open_first')}`")
        lines.append(f"- source_total_row_count: `{sprint.get('source_total_row_count')}`")
        lines.append(f"- unselected_source_row_count: `{sprint.get('unselected_source_row_count')}`")
        lines.append(f"- row_selector: {sprint.get('row_selector')}")
        lines.append(f"- required_human_fields: `{sprint.get('required_human_fields')}`")
        lines.append(f"- decision_options: `{sprint.get('decision_options')}`")
        lines.append("- selected_rows:")
        for row in sprint.get("rows") or []:
            lines.append(
                f"  - `{row.get('source_row_key')}` source_line=`{row.get('source_row_number')}` "
                f"selected_order=`{row.get('selected_order')}` "
                f"scope_ok=`{row.get('scope_match_ok')}` "
                f"blank_ok=`{row.get('decision_fields_blank_ok')}` "
                f"guard_ok=`{row.get('guard_fields_false_ok')}` "
                f"crosswalk_ok=`{row.get('crosswalk_contract_ok')}` {row.get('row_preview')}"
            )
        lines.append("")
    if payload.get("issues"):
        lines.extend(["## Issues", ""])
        for issue in payload.get("issues") or []:
            lines.append(f"- `{issue.get('code')}`: {issue.get('message')}")
    else:
        lines.append("No operator decision workbench issues found.")
    if payload.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        for warning in payload.get("warnings") or []:
            lines.append(f"- `{warning.get('code')}`: `{warning}`")
    lines.extend(["", "## Notes", ""])
    lines.extend(f"- {note}" for note in payload.get("notes") or [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build AI-HR operator decision workbench.")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--sprint-queue", type=Path, required=True)
    parser.add_argument("--entrypoint-manifest", type=Path)
    parser.add_argument("--per-sprint-limit", type=int, default=10)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--csv-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_workbench(
        sprint_queue_path=resolve_artifact(args.sprint_queue, root=args.root) or args.sprint_queue,
        entrypoint_manifest_path=resolve_artifact(args.entrypoint_manifest, root=args.root)
        if args.entrypoint_manifest
        else None,
        root=args.root,
        per_sprint_limit=args.per_sprint_limit,
    )
    write_json(args.out, report)
    if args.markdown_out:
        write_markdown(args.markdown_out, report)
    if args.csv_out:
        write_csv(args.csv_out, report.get("workbench_rows") or [])
    print(
        json.dumps(
            {
                "ok": report.get("ok"),
                "schema": report.get("schema"),
                "status": report.get("status"),
                "sprint_count": report.get("summary", {}).get("sprint_count"),
                "workbench_row_count": report.get("summary", {}).get("workbench_row_count"),
                "issue_count": report.get("summary", {}).get("issue_count"),
                "warning_count": report.get("summary", {}).get("warning_count"),
                "out_path": str(args.out),
                "markdown_path": str(args.markdown_out) if args.markdown_out else None,
                "csv_path": str(args.csv_out) if args.csv_out else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if args.strict and report.get("ok") is not True else 0


if __name__ == "__main__":
    raise SystemExit(main())
