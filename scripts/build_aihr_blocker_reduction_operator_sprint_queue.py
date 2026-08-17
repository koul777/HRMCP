from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS = PROJECT_ROOT / "reports"
INPUT_DECISION_FIELDS = (
    "decision",
    "approved_definition",
    "reviewer_id",
    "reviewed_at",
    "rationale",
    "source_decision_packet",
    "evidence_refs_json",
)
CYCLE_PRONE_CONTEXT_KEYS = (
    "operator_next_actions",
    "lineage_sync_audit",
    "operator_packet_integrity_audit",
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


def artifact_status(path: str | Path, *, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    p = resolve_existing_path(str(path), root=root) or Path(path)
    return {
        "path": portable_path(p, root=root),
        "exists_nonempty": p.exists() and p.is_file() and p.stat().st_size > 0,
        "sha256": sha256_file(p),
    }


def context_artifact(path: Path | None, *, root: Path = PROJECT_ROOT) -> dict[str, Any] | None:
    if path is None:
        return None
    return {
        "path": portable_path(path, root=root),
        "exists_nonempty": path.exists() and path.is_file() and path.stat().st_size > 0,
        "hash_checked": False,
        "role": "context_only_not_queue_source",
    }


def counter_by(rows: list[dict[str, str]], field: str) -> dict[str, int]:
    return dict(Counter(row.get(field, "") for row in rows if row.get(field, "")))


def ids(rows: list[dict[str, str]], field: str, *, limit: int = 10) -> list[str]:
    return [str(row.get(field) or "") for row in rows[:limit]]


def select_by_issue(rows: list[dict[str, str]], issue_types: set[str]) -> list[dict[str, str]]:
    return [row for row in rows if row.get("issue_type") in issue_types]


def ordered_transition_scenarios(rows: list[dict[str, str]]) -> list[str]:
    by_scenario: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        scenario_id = str(row.get("scenario_id") or "").strip()
        if scenario_id:
            by_scenario[scenario_id].append(row)

    def sort_key(scenario_id: str) -> tuple[int, int | str]:
        scenario_rows = by_scenario[scenario_id]
        has_audit = any(str(row.get("audit_id") or "").strip() for row in scenario_rows)
        numeric = int(scenario_id) if scenario_id.isdigit() else scenario_id
        return (0 if has_audit else 1, numeric)

    return sorted(by_scenario, key=sort_key)


def resolve_existing_path(value: str | None, *, root: Path = PROJECT_ROOT) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    rooted = root / path
    if rooted.exists():
        return rooted
    if path.exists():
        return path
    return rooted


def csv_decision_field_nonblank_counts(path: Path) -> dict[str, int]:
    if not path.exists() or path.suffix.lower() != ".csv":
        return {}
    counts: dict[str, int] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        checked = [field for field in INPUT_DECISION_FIELDS if field in fields]
        for row in reader:
            for field in checked:
                if str(row.get(field) or "").strip():
                    counts[field] = counts.get(field, 0) + 1
    return counts


def surface_order_ranges(rows: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    ranges: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, start=1):
        surface = str(row.get("surface") or "").strip() or "unknown"
        entry = ranges.setdefault(surface, {"start": index, "end": index, "count": 0})
        entry["end"] = index
        entry["count"] += 1
    return ranges


def transition_decision_mapping(
    *,
    transition_scenarios: list[str],
    transition_scenario_rows: list[dict[str, str]],
) -> dict[str, Any]:
    rows_by_scenario: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in transition_scenario_rows:
        scenario_id = str(row.get("target_id") or "").strip()
        if scenario_id:
            rows_by_scenario[scenario_id].append(row)

    missing = [
        scenario_id
        for scenario_id in transition_scenarios
        if not rows_by_scenario.get(scenario_id)
    ]
    duplicates = [
        scenario_id
        for scenario_id in transition_scenarios
        if len(rows_by_scenario.get(scenario_id, [])) > 1
    ]
    return {
        "ok": not missing and not duplicates,
        "scenario_count": len(transition_scenarios),
        "decision_sheet_transition_row_count": len(transition_scenario_rows),
        "matched_scenario_ids": [
            scenario_id
            for scenario_id in transition_scenarios
            if len(rows_by_scenario.get(scenario_id, [])) == 1
        ],
        "missing_decision_sheet_scenario_ids": missing,
        "duplicate_decision_sheet_scenario_ids": duplicates,
        "decision_only_scenario_ids": sorted(
            set(rows_by_scenario) - set(transition_scenarios),
            key=lambda value: (0, int(value)) if value.isdigit() else (1, value),
        ),
        "rows": [
            {
                "scenario_id": scenario_id,
                "decision_sheet_row_count": len(rows_by_scenario.get(scenario_id, [])),
                "decision_sheet_orders": [
                    str(row.get("order") or "").strip()
                    for row in rows_by_scenario.get(scenario_id, [])
                ],
            }
            for scenario_id in transition_scenarios
        ],
    }


def build_queue(
    *,
    concept_seedpack_csv: Path,
    blocker_ranked_seedpack_csv: Path,
    provenance_decision_sheet_csv: Path,
    transition_gap_csv: Path,
    qualification_decision_csv: Path,
    operator_next_actions_json: Path | None = None,
    lineage_sync_audit_json: Path | None = None,
    operator_packet_integrity_audit_json: Path | None = None,
    transition_crosswalk_csv: Path | None = None,
    transition_crosswalk_audit_json: Path | None = None,
    generated_at: str | None = None,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    def ppath(path: str | Path) -> str:
        return portable_path(path, root=root)

    def status(path: str | Path) -> dict[str, Any]:
        return artifact_status(path, root=root)

    concept_rows = read_csv_rows(concept_seedpack_csv)
    blocker_rows = read_csv_rows(blocker_ranked_seedpack_csv)
    provenance_rows = read_csv_rows(provenance_decision_sheet_csv)
    transition_rows = read_csv_rows(transition_gap_csv)
    qualification_rows = read_csv_rows(qualification_decision_csv)

    transition_scenarios = ordered_transition_scenarios(transition_rows)
    transition_scenario_rows = [
        row
        for row in provenance_rows
        if row.get("surface") == "training_transition_gold_scenarios"
    ]
    transition_mapping = transition_decision_mapping(
        transition_scenarios=transition_scenarios,
        transition_scenario_rows=transition_scenario_rows,
    )
    alias_rows = [row for row in provenance_rows if row.get("surface") == "ncs_query_aliases"]
    career_rows = [row for row in provenance_rows if row.get("surface") == "ncs_career_paths"]
    task_rows = select_by_issue(
        blocker_rows,
        {"ontology_task_ksa_relation_human_review_required"},
    )
    hr_goal_rows = select_by_issue(
        blocker_rows,
        {"hr_training_goal_link_human_review_required"},
    )
    concept_first_rows = concept_rows[:10]

    source_paths: dict[str, str | None] = {
        "concept_seedpack_csv": ppath(concept_seedpack_csv),
        "blocker_ranked_seedpack_csv": ppath(blocker_ranked_seedpack_csv),
        "provenance_decision_sheet_csv": ppath(provenance_decision_sheet_csv),
        "transition_trusted_scenario_provenance_gap_csv": ppath(transition_gap_csv),
        "qualification_guarded_batch_decision_csv": ppath(qualification_decision_csv),
        "transition_provenance_crosswalk_csv": (
            ppath(transition_crosswalk_csv) if transition_crosswalk_csv else None
        ),
        "transition_provenance_crosswalk_audit": (
            ppath(transition_crosswalk_audit_json)
            if transition_crosswalk_audit_json
            else None
        ),
    }
    source_path_objects = {
        "concept_seedpack_csv": concept_seedpack_csv,
        "blocker_ranked_seedpack_csv": blocker_ranked_seedpack_csv,
        "provenance_decision_sheet_csv": provenance_decision_sheet_csv,
        "transition_trusted_scenario_provenance_gap_csv": transition_gap_csv,
        "qualification_guarded_batch_decision_csv": qualification_decision_csv,
        "transition_provenance_crosswalk_csv": transition_crosswalk_csv,
        "transition_provenance_crosswalk_audit": transition_crosswalk_audit_json,
    }
    source_hashes = {
        key: sha256_file(path) if path else None for key, path in source_path_objects.items()
    }
    context_artifacts = {
        "operator_next_actions": context_artifact(operator_next_actions_json, root=root),
        "lineage_sync_audit": context_artifact(lineage_sync_audit_json, root=root),
        "operator_packet_integrity_audit": context_artifact(
            operator_packet_integrity_audit_json,
            root=root,
        ),
    }

    provenance_ranges = surface_order_ranges(provenance_rows)
    transition_range = provenance_ranges.get("training_transition_gold_scenarios", {})
    alias_range = provenance_ranges.get("ncs_query_aliases", {})
    career_range = provenance_ranges.get("ncs_career_paths", {})

    transition_open_first = transition_crosswalk_csv or transition_gap_csv
    transition_artifacts = [
        ppath(transition_open_first),
        ppath(transition_gap_csv),
        ppath(provenance_decision_sheet_csv),
        ppath(transition_gap_csv.with_suffix(".md")),
        ppath(provenance_decision_sheet_csv.with_suffix(".md")),
    ]
    if transition_crosswalk_csv:
        transition_artifacts.append(ppath(transition_crosswalk_csv.with_suffix(".md")))
    if transition_crosswalk_audit_json:
        transition_artifacts.extend(
            [
                ppath(transition_crosswalk_audit_json),
                ppath(transition_crosswalk_audit_json.with_suffix(".md")),
            ]
        )

    queue = [
        {
            "rank": 1,
            "sprint_id": "S1-transition-provenance-crosswalk",
            "blocker": (
                "transition_eval:trusted_scenarios + "
                "human_review:provenance_reconfirmation_required"
            ),
            "operator_goal": (
                "Review transition scenario provenance gaps together with matching "
                "provenance decision-sheet rows so one packet-backed decision bundle can "
                "reduce both blockers."
            ),
            "open_first": ppath(transition_open_first),
            "artifacts_to_open": transition_artifacts,
            "row_selector": (
                "transition_gap: scenario ids "
                f"{','.join(transition_scenarios)}; provenance decision sheet: "
                f"training_transition_gold_scenarios rows {transition_range.get('start')}-"
                f"{transition_range.get('end')}"
            ),
            "row_count": len(transition_scenarios),
            "next_safe_action": (
                "review-transition-provenance-crosswalk-human-decisions"
            ),
            "first_row_ids": transition_scenarios,
            "required_human_fields": [
                "decision",
                "rationale",
                "reviewer_id",
                "reviewed_at",
                "source_decision_packet",
                "evidence_refs_json",
            ],
            "decision_options": ["reconfirm", "downgrade_to_review_required", "defer"],
            "forbidden": [
                "trust scenario automatically",
                "reconfirm no_audit_log scenarios casually",
                "status_update_allowed=true",
                "db_writes=true",
                "automatic human_reviewed/accepted/reviewed",
            ],
            "evidence_note": (
                "Use the transition provenance crosswalk when present; it maps each "
                "scenario_id to the provenance decision row and packet hash. Fill the "
                "provenance sheet only after a human reviewer decides."
            ),
            "estimated_operator_batch": (
                f"{len(transition_scenarios)} scenario groups / provenance rows "
                f"{transition_range.get('start')}-{transition_range.get('end')}"
            ),
        },
        {
            "rank": 2,
            "sprint_id": "S2-provenance-alias-career-cleanup",
            "blocker": "human_review:provenance_reconfirmation_required",
            "operator_goal": (
                "After transition scenario rows, finish remaining provenance "
                "reconfirmation rows: query aliases first, then career paths."
            ),
            "open_first": ppath(provenance_decision_sheet_csv),
            "artifacts_to_open": [
                ppath(provenance_decision_sheet_csv),
                ppath(provenance_decision_sheet_csv.with_suffix(".md")),
            ],
            "row_selector": (
                f"rows {alias_range.get('start')}-{alias_range.get('end')} "
                "ncs_query_aliases first, then rows "
                f"{career_range.get('start')}-{career_range.get('end')} ncs_career_paths"
            ),
            "row_count": len(alias_rows) + len(career_rows),
            "next_safe_action": (
                "review-provenance-alias-career-decision-sheet-human-decisions"
            ),
            "first_row_ids": ids(alias_rows, "order", limit=5) + ids(career_rows, "order", limit=5),
            "required_human_fields": [
                "decision",
                "rationale",
                "reviewer_id",
                "reviewed_at",
                "source_decision_packet",
                "evidence_refs_json",
            ],
            "decision_options": ["reconfirm", "downgrade_to_review_required", "defer"],
            "forbidden": [
                "status_update_allowed=true",
                "db_writes=true",
                "approval_claim=true",
                "automatic human_reviewed/accepted/reviewed",
            ],
            "evidence_note": (
                "These rows share the same provenance problem; reconfirm is still only "
                "evidence-review input for a later guarded workflow."
            ),
            "estimated_operator_batch": f"{len(alias_rows) + len(career_rows)} provenance rows",
        },
        {
            "rank": 3,
            "sprint_id": "S5-core-concept-definition-review",
            "blocker": "review_debt:human_reviewed_concepts",
            "operator_goal": (
                "Review the highest-priority AI-HR concept rows before any concept can "
                "count as human reviewed."
            ),
            "open_first": ppath(concept_seedpack_csv),
            "artifacts_to_open": [
                ppath(concept_seedpack_csv),
                ppath(concept_seedpack_csv.with_suffix(".md")),
                ppath(concept_seedpack_csv.with_suffix(".jsonl")),
            ],
            "row_selector": "start with rows 1-10, then continue by priority_score",
            "row_count": len(concept_first_rows),
            "next_safe_action": "review-core-concept-definition-decision-surface",
            "first_row_ids": ids(concept_first_rows, "sequence", limit=10),
            "required_human_fields": ["decision", "reviewer_id", "reviewed_at", "rationale"],
            "decision_options": ["accept_concept", "revise_definition", "reject_concept", "defer"],
            "forbidden": [
                "promote boilerplate definitions",
                "copy draft_definition automatically",
                "automatic human_reviewed status",
            ],
            "evidence_note": (
                "Definitions are review assistance only until a human-approved, separately "
                "audited apply step exists."
            ),
            "estimated_operator_batch": "first 10 AI-HR concept rows",
        },
        {
            "rank": 4,
            "sprint_id": "S4-task-ksa-relation-review",
            "blocker": "review_debt:human_reviewed_task_relations",
            "operator_goal": (
                "Review task/KSA relation rows so task evidence can be trusted in "
                "explanations and matrix rows."
            ),
            "open_first": ppath(blocker_ranked_seedpack_csv),
            "artifacts_to_open": [
                ppath(blocker_ranked_seedpack_csv),
                ppath(blocker_ranked_seedpack_csv.with_suffix(".md")),
            ],
            "row_selector": (
                "filter issue_type=ontology_task_ksa_relation_human_review_required"
            ),
            "row_count": len(task_rows),
            "next_safe_action": "review-task-ksa-relation-decision-surface",
            "first_row_ids": ids(task_rows, "sequence", limit=10),
            "required_human_fields": ["decision", "reviewer_id", "reviewed_at", "rationale"],
            "decision_options": [
                "accept_relation",
                "reject_relation",
                "needs_more_evidence",
                "defer",
            ],
            "forbidden": [
                "modify ksa_items.ksa_text_raw",
                "automatic status promotion",
                "DB writes from queue",
            ],
            "evidence_note": (
                "Task/KSA relation decisions must preserve raw KSA text and only act "
                "through guarded review tooling later."
            ),
            "estimated_operator_batch": f"{len(task_rows)} task/KSA rows",
        },
        {
            "rank": 5,
            "sprint_id": "S3-training-goal-link-review",
            "blocker": "review_debt:human_reviewed_goal_links",
            "operator_goal": (
                "Review HR training-goal to KSA links that affect recommendation ranking "
                "and explanations."
            ),
            "open_first": ppath(blocker_ranked_seedpack_csv),
            "artifacts_to_open": [
                ppath(blocker_ranked_seedpack_csv),
                ppath(blocker_ranked_seedpack_csv.with_suffix(".md")),
            ],
            "row_selector": (
                "filter issue_type=hr_training_goal_link_human_review_required; defer "
                "non-HR or concept-overlap rows until after dedicated concept review"
            ),
            "row_count": len(hr_goal_rows),
            "next_safe_action": "review-training-goal-link-decision-surface",
            "first_row_ids": ids(hr_goal_rows, "sequence", limit=10),
            "required_human_fields": ["decision", "reviewer_id", "reviewed_at", "rationale"],
            "decision_options": [
                "accept_link",
                "reject_link",
                "needs_more_evidence",
                "defer",
            ],
            "forbidden": [
                "direct DB update from seedpack",
                "automatic status promotion",
                "treat suggested_action as approval",
            ],
            "evidence_note": (
                "Seedpack is a review surface only; status changes require a separate "
                "controlled review workflow."
            ),
            "estimated_operator_batch": f"{len(hr_goal_rows)} HR training-goal link rows",
        },
        {
            "rank": 6,
            "sprint_id": "S6-qualification-coverage-pilot-window",
            "blocker": "qualification:collection_coverage",
            "operator_goal": (
                "Choose whether to run a guarded qualification API pilot window; this "
                "queue does not execute collection."
            ),
            "open_first": ppath(qualification_decision_csv),
            "artifacts_to_open": [
                ppath(qualification_decision_csv),
                ppath(qualification_decision_csv.with_suffix(".md")),
            ],
            "row_selector": (
                "pilot wave first; run only if operator explicitly authorizes timing"
            ),
            "row_count": len(qualification_rows),
            "next_safe_action": "plan-guarded-qualification-pilot-window",
            "first_row_ids": ids(qualification_rows, "wave", limit=3),
            "required_human_fields": [
                "operator timing approval",
                "batch count",
                "stop conditions",
                "post-run verification owner",
            ],
            "decision_options": ["run_pilot_window", "defer", "reduce_batch_count"],
            "forbidden": [
                "automatic queue execution",
                "running broad collection from this report",
                "status_update_allowed=true",
            ],
            "evidence_note": (
                "Retry hygiene is clear for retry candidates only; coverage collection "
                "still requires operator timing."
            ),
            "estimated_operator_batch": "3-batch pilot planning window, no API call here",
        },
    ]

    for item in queue:
        item["artifacts_status"] = [status(path) for path in item["artifacts_to_open"]]
        item["open_first_exists_nonempty"] = status(item["open_first"])["exists_nonempty"]

    queue_ok = bool(transition_mapping["ok"])

    return {
        "schema": "aihr_blocker_reduction_operator_sprint_queue_v1",
        "generated_at": generated_at or now_iso(),
        "ok": queue_ok,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "human_decision_required": True,
        "forbidden_automatic_statuses": ["human_reviewed", "accepted", "reviewed"],
        "source_paths": source_paths,
        "source_hashes": source_hashes,
        "context_artifacts": context_artifacts,
        "cycle_avoidance_contract": {
            "context_only_keys": list(CYCLE_PRONE_CONTEXT_KEYS),
            "source_hashes_exclude_context_only_keys": True,
            "reason": (
                "operator next-actions, packet integrity, and lineage sync artifacts are "
                "downstream of this sprint queue and must not become queue source hashes."
            ),
        },
        "inputs_summary": {
            "concept_seedpack_rows": len(concept_rows),
            "concept_issue_counts": counter_by(concept_rows, "issue_type"),
            "blocker_ranked_seedpack_rows": len(blocker_rows),
            "blocker_issue_counts": counter_by(blocker_rows, "issue_type"),
            "provenance_decision_rows": len(provenance_rows),
            "provenance_surface_counts": counter_by(provenance_rows, "surface"),
            "transition_gap_rows": len(transition_rows),
            "transition_unique_scenario_count": len(transition_scenarios),
            "transition_unique_scenario_ids": transition_scenarios,
            "transition_provenance_decision_mapping_ok": transition_mapping["ok"],
            "qualification_wave_rows": len(qualification_rows),
        },
        "transition_provenance_decision_mapping": transition_mapping,
        "queue": queue,
        "queue_count": len(queue),
        "operator_sprint_order_basis": (
            "Prioritizes overlapping transition/provenance rows first, then cheap "
            "alias/career provenance cleanup, then concept/task/goal-link review "
            "slices, with qualification API timing last."
        ),
        "acceptance_contract": {
            "all_open_first_exist": all(item["open_first_exists_nonempty"] for item in queue),
            "all_artifacts_exist": all(
                status["exists_nonempty"]
                for item in queue
                for status in item["artifacts_status"]
            ),
            "decision_fields_blank_until_human_review": True,
            "transition_scenarios_have_decision_sheet_rows": not transition_mapping[
                "missing_decision_sheet_scenario_ids"
            ],
            "transition_scenario_decision_rows_unique": not transition_mapping[
                "duplicate_decision_sheet_scenario_ids"
            ],
            "does_not_authorize_apply": True,
            "does_not_authorize_api_collection": True,
        },
    }


def audit_queue(
    report: dict[str, Any],
    *,
    source_path: Path | None = None,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if report.get("ok") is not True:
        issues.append({"code": "report_ok_not_true", "value": report.get("ok")})
    for field in ("status_update_allowed", "db_writes", "api_calls", "approval_claim"):
        if report.get(field) is not False:
            issues.append({"code": f"{field}_not_false", "value": report.get(field)})
    if report.get("report_only") is not True:
        issues.append({"code": "report_only_not_true", "value": report.get("report_only")})
    if report.get("human_decision_required") is not True:
        issues.append(
            {
                "code": "human_decision_required_not_true",
                "value": report.get("human_decision_required"),
            }
        )
    for status in ("human_reviewed", "accepted", "reviewed"):
        if status not in (report.get("forbidden_automatic_statuses") or []):
            issues.append({"code": "missing_forbidden_automatic_status", "status": status})
    for item in report.get("queue") or []:
        if not isinstance(item, dict):
            issues.append({"code": "queue_item_not_object"})
            continue
        if item.get("open_first_exists_nonempty") is not True:
            issues.append(
                {
                    "code": "open_first_missing",
                    "sprint_id": item.get("sprint_id"),
                    "path": item.get("open_first"),
                }
            )
        if not item.get("required_human_fields"):
            issues.append(
                {"code": "required_human_fields_missing", "sprint_id": item.get("sprint_id")}
            )
    contract = report.get("acceptance_contract") if isinstance(report.get("acceptance_contract"), dict) else {}
    for field in (
        "all_open_first_exist",
        "all_artifacts_exist",
        "decision_fields_blank_until_human_review",
        "transition_scenarios_have_decision_sheet_rows",
        "transition_scenario_decision_rows_unique",
        "does_not_authorize_apply",
        "does_not_authorize_api_collection",
    ):
        if contract.get(field) is not True:
            issues.append({"code": f"acceptance_{field}_not_true", "value": contract.get(field)})
    transition_mapping = (
        report.get("transition_provenance_decision_mapping")
        if isinstance(report.get("transition_provenance_decision_mapping"), dict)
        else {}
    )
    missing_transition_rows = transition_mapping.get("missing_decision_sheet_scenario_ids") or []
    duplicate_transition_rows = transition_mapping.get("duplicate_decision_sheet_scenario_ids") or []
    if missing_transition_rows:
        issues.append(
            {
                "code": "transition_scenarios_missing_decision_sheet_rows",
                "scenario_ids": missing_transition_rows,
            }
        )
    if duplicate_transition_rows:
        issues.append(
            {
                "code": "transition_scenario_duplicate_decision_sheet_rows",
                "scenario_ids": duplicate_transition_rows,
            }
        )
    source_paths = report.get("source_paths") if isinstance(report.get("source_paths"), dict) else {}
    source_hashes = report.get("source_hashes") if isinstance(report.get("source_hashes"), dict) else {}
    source_hash_checks: dict[str, dict[str, Any]] = {}
    input_decision_field_checks: dict[str, dict[str, Any]] = {}
    cycle_prone_source_keys = [
        key for key in CYCLE_PRONE_CONTEXT_KEYS if source_paths.get(key)
    ]
    if cycle_prone_source_keys:
        issues.append(
            {
                "code": "cycle_prone_context_keys_in_source_paths",
                "source_keys": cycle_prone_source_keys,
            }
        )
    cycle_contract = (
        report.get("cycle_avoidance_contract")
        if isinstance(report.get("cycle_avoidance_contract"), dict)
        else {}
    )
    if cycle_contract.get("source_hashes_exclude_context_only_keys") is not True:
        issues.append(
            {
                "code": "cycle_avoidance_contract_missing_or_false",
                "value": cycle_contract.get("source_hashes_exclude_context_only_keys"),
            }
        )
    for key, value in source_paths.items():
        path = resolve_existing_path(value if isinstance(value, str) else None, root=root)
        if path is None:
            continue
        current_hash = sha256_file(path)
        expected_hash = source_hashes.get(key)
        exists_nonempty = path.exists() and path.is_file() and path.stat().st_size > 0
        source_hash_checks[key] = {
            "path": portable_path(path, root=root),
            "exists_nonempty": exists_nonempty,
            "expected_sha256": expected_hash,
            "current_sha256": current_hash,
            "hash_matches": expected_hash == current_hash,
        }
        if not exists_nonempty:
            issues.append({"code": "source_artifact_missing", "source_key": key, "path": str(path)})
        elif expected_hash and expected_hash != current_hash:
            issues.append(
                {
                    "code": "source_artifact_hash_mismatch",
                    "source_key": key,
                    "expected_sha256": expected_hash,
                    "current_sha256": current_hash,
                }
            )
        nonblank_counts = csv_decision_field_nonblank_counts(path)
        if nonblank_counts:
            input_decision_field_checks[key] = {
                "path": portable_path(path, root=root),
                "nonblank_counts": nonblank_counts,
                "blank_ok": False,
            }
            issues.append(
                {
                    "code": "input_decision_fields_not_blank",
                    "source_key": key,
                    "nonblank_counts": nonblank_counts,
                }
            )
        elif path.suffix.lower() == ".csv":
            input_decision_field_checks[key] = {
                "path": portable_path(path, root=root),
                "nonblank_counts": {},
                "blank_ok": True,
            }
    return {
        "schema": "aihr_blocker_reduction_operator_sprint_queue_audit_v1",
        "generated_at": now_iso(),
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "ok": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "source_path": portable_path(source_path, root=root) if source_path else None,
        "source_sha256": sha256_file(source_path) if source_path else None,
        "queue_count": report.get("queue_count"),
        "acceptance_contract": contract,
        "cycle_avoidance_contract": cycle_contract,
        "transition_provenance_decision_mapping": transition_mapping,
        "source_hash_checks": source_hash_checks,
        "input_decision_field_checks": input_decision_field_checks,
    }


def write_queue_csv(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "rank",
        "sprint_id",
        "blocker",
        "operator_goal",
        "open_first",
        "row_selector",
        "row_count",
        "required_human_fields",
        "decision_options",
        "forbidden",
        "estimated_operator_batch",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in report.get("queue") or []:
            writer.writerow(
                {
                    field: json.dumps(item.get(field), ensure_ascii=False)
                    if isinstance(item.get(field), list)
                    else item.get(field)
                    for field in fields
                }
            )


def write_queue_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# AI-HR Blocker Reduction Operator Sprint Queue",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- ok: `{report.get('ok')}`",
        f"- report_only: `{report.get('report_only')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- api_calls: `{report.get('api_calls')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        f"- queue_count: `{report.get('queue_count')}`",
        f"- order_basis: {report.get('operator_sprint_order_basis')}",
        "",
        "## Input Summary",
    ]
    for key, value in (report.get("inputs_summary") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Sprint Queue"])
    for item in report.get("queue") or []:
        lines.extend(
            [
                f"### {item.get('rank')}. {item.get('sprint_id')}",
                f"- blocker: `{item.get('blocker')}`",
                f"- open_first: `{item.get('open_first')}`",
                f"- row_selector: {item.get('row_selector')}",
                f"- row_count: `{item.get('row_count')}`; first_row_ids: `{item.get('first_row_ids')}`",
                f"- required_human_fields: `{item.get('required_human_fields')}`",
                f"- decision_options: `{item.get('decision_options')}`",
                f"- forbidden: `{item.get('forbidden')}`",
                f"- note: {item.get('evidence_note')}",
                "",
            ]
        )
    lines.extend(["## Acceptance Contract"])
    for key, value in (report.get("acceptance_contract") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "No human_reviewed, accepted, or reviewed status is authorized by this queue."])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_audit_markdown(path: Path, audit: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# AI-HR Blocker Reduction Operator Sprint Queue Audit",
        "",
        f"- schema: `{audit.get('schema')}`",
        f"- generated_at: `{audit.get('generated_at')}`",
        f"- ok: `{audit.get('ok')}`",
        f"- issue_count: `{audit.get('issue_count')}`",
        f"- report_only: `{audit.get('report_only')}`",
        f"- status_update_allowed: `{audit.get('status_update_allowed')}`",
        f"- db_writes: `{audit.get('db_writes')}`",
        f"- api_calls: `{audit.get('api_calls')}`",
        f"- approval_claim: `{audit.get('approval_claim')}`",
        f"- source_sha256: `{audit.get('source_sha256')}`",
        "",
        "## Acceptance Contract",
    ]
    for key, value in (audit.get("acceptance_contract") or {}).items():
        lines.append(f"- {key}: `{value}`")
    if audit.get("issues"):
        lines.extend(["", "## Issues"])
        for issue in audit.get("issues") or []:
            lines.append(f"- `{issue.get('code')}`")
    else:
        lines.extend(["", "No sprint queue integrity issues found."])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def input_or_latest(
    value: Path | None,
    *patterns: str,
    reports_dir: Path = REPORTS,
) -> Path:
    return value if value is not None else latest_report_path(*patterns, reports_dir=reports_dir)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the AI-HR blocker-reduction operator sprint queue."
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--concept-seedpack-csv",
        type=Path,
    )
    parser.add_argument(
        "--blocker-ranked-seedpack-csv",
        type=Path,
    )
    parser.add_argument(
        "--provenance-decision-sheet-csv",
        type=Path,
    )
    parser.add_argument(
        "--transition-gap-csv",
        type=Path,
    )
    parser.add_argument(
        "--qualification-decision-csv",
        type=Path,
    )
    parser.add_argument(
        "--operator-next-actions-json",
        type=Path,
    )
    parser.add_argument(
        "--lineage-sync-audit-json",
        type=Path,
    )
    parser.add_argument(
        "--operator-packet-integrity-audit-json",
        type=Path,
    )
    parser.add_argument(
        "--transition-crosswalk-csv",
        type=Path,
    )
    parser.add_argument(
        "--transition-crosswalk-audit-json",
        type=Path,
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--csv-out", type=Path)
    parser.add_argument("--audit-out", type=Path)
    parser.add_argument("--audit-markdown-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    reports_dir = args.root / "reports"

    concept_seedpack_csv = input_or_latest(
        args.concept_seedpack_csv,
        "aihr_ontology_definition_review_seedpack_*.csv",
        reports_dir=reports_dir,
    )
    blocker_ranked_seedpack_csv = input_or_latest(
        args.blocker_ranked_seedpack_csv,
        "aihr_review_seedpack_blocker_ranked_*.csv",
        reports_dir=reports_dir,
    )
    provenance_decision_sheet_csv = input_or_latest(
        args.provenance_decision_sheet_csv,
        "human_review_provenance_reconfirmation_decision_sheet_*.csv",
        reports_dir=reports_dir,
    )
    transition_gap_csv = input_or_latest(
        args.transition_gap_csv,
        "transition_trusted_scenario_provenance_gap_*.csv",
        reports_dir=reports_dir,
    )
    qualification_decision_csv = input_or_latest(
        args.qualification_decision_csv,
        "qualification_guarded_batch_operator_decision_*.csv",
        reports_dir=reports_dir,
    )
    report = build_queue(
        concept_seedpack_csv=concept_seedpack_csv,
        blocker_ranked_seedpack_csv=blocker_ranked_seedpack_csv,
        provenance_decision_sheet_csv=provenance_decision_sheet_csv,
        transition_gap_csv=transition_gap_csv,
        qualification_decision_csv=qualification_decision_csv,
        operator_next_actions_json=args.operator_next_actions_json,
        lineage_sync_audit_json=args.lineage_sync_audit_json,
        operator_packet_integrity_audit_json=args.operator_packet_integrity_audit_json,
        transition_crosswalk_csv=args.transition_crosswalk_csv,
        transition_crosswalk_audit_json=args.transition_crosswalk_audit_json,
        root=args.root,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out:
        write_queue_markdown(args.markdown_out, report)
    if args.csv_out:
        write_queue_csv(args.csv_out, report)

    audit: dict[str, Any] | None = None
    if args.audit_out or args.audit_markdown_out or args.strict:
        audit = audit_queue(report, source_path=args.out, root=args.root)
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
                "queue_count": report.get("queue_count"),
                "out": str(args.out),
                "markdown_out": str(args.markdown_out) if args.markdown_out else None,
                "csv_out": str(args.csv_out) if args.csv_out else None,
                "audit_out": str(args.audit_out) if args.audit_out else None,
                "audit_ok": audit.get("ok") if audit else None,
                "audit_issue_count": audit.get("issue_count") if audit else None,
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
