from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "aihr_post_decision_validation_matrix_v1"
FORBIDDEN_AUTOMATIC_STATUSES = ["human_reviewed", "accepted", "reviewed"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_artifact(value: str | Path | None, *, root: Path = PROJECT_ROOT) -> Path | None:
    text = str(value or "").partition("#")[0].strip()
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
    text = str(path or "").partition("#")[0].strip()
    if not text:
        return None
    resolved = Path(text).resolve(strict=False)
    try:
        return resolved.relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists() or not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, dict) else {}


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def source_status(path: Path | None, *, root: Path) -> dict[str, Any]:
    return {
        "path": portable_path(path, root=root),
        "exists_nonempty": bool(
            path and path.exists() and path.is_file() and path.stat().st_size > 0
        ),
        "sha256": sha256_file(path),
    }


def safe_contract(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        "report_only_is_true_or_absent": payload.get("report_only") in (True, None),
        "status_update_allowed_is_false": payload.get("status_update_allowed") is False,
        "db_writes_is_false": payload.get("db_writes") is False,
        "api_calls_is_false_or_absent": payload.get("api_calls") in (False, None),
        "approval_claim_is_false": payload.get("approval_claim") is False,
        "acceptance_claim_is_false_or_absent": payload.get("acceptance_claim") in (False, None),
        "human_decision_required_is_true_or_absent": payload.get("human_decision_required")
        in (True, None),
    }


def bool_all(contract: dict[str, bool]) -> bool:
    return all(value is True for value in contract.values())


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def int_value(value: Any) -> int:
    return int_or_none(value) or 0


def issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    record = {"code": code, "message": message}
    record.update({key: value for key, value in details.items() if value is not None})
    return record


def blocker_tokens(value: Any) -> set[str]:
    tokens: set[str] = set()
    for chunk in str(value or "").split("+"):
        for part in chunk.split(","):
            text = part.strip()
            if text:
                tokens.add(text)
    return tokens


def workbench_scope(blocker: str, payload: dict[str, Any]) -> dict[str, Any]:
    sprints = [
        item
        for item in (payload.get("sprints") or [])
        if isinstance(item, dict) and blocker in blocker_tokens(item.get("blocker"))
    ]
    records = [
        {
            "sprint_id": item.get("sprint_id"),
            "blocker": item.get("blocker"),
            "source_path": item.get("source_path"),
            "row_selector": item.get("row_selector"),
            "selected_row_count": int_value(item.get("selected_row_count") or item.get("row_count")),
        }
        for item in sprints
    ]
    return {
        "sprint_count": len(records),
        "selected_row_count": sum(item["selected_row_count"] for item in records),
        "sprints": records,
    }


def audit_summary(payload: dict[str, Any]) -> dict[str, Any]:
    def first_present(*keys: str) -> Any:
        for key in keys:
            if key in payload:
                return payload.get(key)
        return None

    return {
        "schema": payload.get("schema"),
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "row_count": payload.get("row_count"),
        "pending_decision_count": first_present("pending_decision_count", "pending_count"),
        "completed_decision_count": first_present("completed_decision_count", "completed_count"),
        "invalid_decision_count": first_present("invalid_decision_count", "invalid_count"),
        "guard_issue_row_count": payload.get("guard_issue_row_count"),
        "action_eligible_count": payload.get("action_eligible_count"),
        "issue_count": payload.get("issue_count"),
        "require_completed_decisions": payload.get("require_completed_decisions"),
        "completion_issue": payload.get("completion_issue"),
        "missing_required_columns": payload.get("missing_required_columns"),
    }


def path_from(value: str, *, root: Path) -> Path:
    return resolve_artifact(value, root=root) or (root / value)


def default_paths(stamp: str, *, root: Path) -> dict[str, Path]:
    reports = root / "reports"
    return {
        "release_blocker_addendum": reports
        / f"aihr_release_blocker_operator_addendum_{stamp}.json",
        "operator_workbench": reports / f"aihr_operator_decision_workbench_{stamp}.json",
        "ontology_seedpack_audit": reports
        / f"aihr_ontology_definition_review_seedpack_decision_audit_{stamp}.json",
        "goal_link_seedpack_audit": reports
        / f"aihr_review_seedpack_goal_link_decision_audit_{stamp}.json",
        "task_relation_seedpack_audit": reports
        / f"aihr_review_seedpack_task_relation_decision_audit_{stamp}.json",
        "provenance_decision_audit": reports
        / f"human_review_provenance_reconfirmation_decision_audit_{stamp}.json",
        "qualification_decision": reports
        / f"qualification_guarded_batch_operator_decision_{stamp}.json",
        "qualification_decision_audit": reports
        / f"qualification_guarded_batch_operator_decision_audit_{stamp}.json",
        "ksa_definition_decision_audit": reports
        / f"ksa_definition_review_operator_packet_{stamp}_decision_audit.json",
        "ksa_definition_action_plan": reports
        / f"ksa_definition_review_operator_packet_{stamp}_action_plan.json",
    }


def validation_rows(*, stamp: str, root: Path) -> list[dict[str, Any]]:
    return [
        {
            "blocker": "review_debt:human_reviewed_concepts",
            "decision_surface": f"reports/aihr_ontology_definition_review_seedpack_{stamp}.csv",
            "post_decision_command": (
                "python scripts\\audit_aihr_review_seedpack_csv_decisions.py "
                f"--csv reports\\aihr_ontology_definition_review_seedpack_{stamp}.csv "
                f"--out reports\\aihr_ontology_definition_review_seedpack_decision_audit_{stamp}.json "
                f"--markdown-out reports\\aihr_ontology_definition_review_seedpack_decision_audit_{stamp}.md "
                "--require-completed-decisions "
                "--strict"
            ),
            "post_decision_audit_key": "ontology_seedpack_audit",
            "supplemental_commands": [
                (
                    "python scripts\\ncs_harness.py audit-ksa-definition-review-decisions "
                    f"--csv reports\\ksa_definition_review_operator_packet_{stamp}_priority_review_pack.csv "
                    f"--source-packet reports\\ksa_definition_review_operator_packet_{stamp}.json "
                    f"--source-review-pack reports\\ksa_definition_review_operator_packet_{stamp}_priority_review_pack.json "
                    f"--out reports\\ksa_definition_review_operator_packet_{stamp}_decision_audit.json "
                    f"--markdown-out reports\\ksa_definition_review_operator_packet_{stamp}_decision_audit.md"
                ),
                (
                    "python scripts\\ncs_harness.py plan-ksa-definition-review-actions "
                    f"--csv reports\\ksa_definition_review_operator_packet_{stamp}_priority_review_pack.csv "
                    f"--source-packet reports\\ksa_definition_review_operator_packet_{stamp}.json "
                    f"--source-review-pack reports\\ksa_definition_review_operator_packet_{stamp}_priority_review_pack.json "
                    f"--out reports\\ksa_definition_review_operator_packet_{stamp}_action_plan.json "
                    f"--markdown-out reports\\ksa_definition_review_operator_packet_{stamp}_action_plan.md"
                ),
            ],
            "notes": "Use the generic seedpack audit for the AI-HR seedpack CSV; use the KSA definition audit/action plan for the dedicated definition packet.",
        },
        {
            "blocker": "review_debt:human_reviewed_goal_links",
            "decision_surface": f"reports/aihr_review_seedpack_blocker_ranked_{stamp}.csv",
            "row_selector": (
                "issue_type in "
                "[hr_training_goal_link_human_review_required, ontology_training_goal_link_human_review_required]"
            ),
            "post_decision_command": (
                "python scripts\\audit_aihr_review_seedpack_csv_decisions.py "
                f"--csv reports\\aihr_review_seedpack_blocker_ranked_{stamp}.csv "
                f"--out reports\\aihr_review_seedpack_goal_link_decision_audit_{stamp}.json "
                f"--markdown-out reports\\aihr_review_seedpack_goal_link_decision_audit_{stamp}.md "
                "--issue-type hr_training_goal_link_human_review_required "
                "--issue-type ontology_training_goal_link_human_review_required "
                "--require-completed-decisions "
                "--strict"
            ),
            "post_decision_audit_key": "goal_link_seedpack_audit",
            "supplemental_commands": [],
            "notes": "Filter the shared blocker-ranked audit rows by goal-link issue type before semantic review.",
        },
        {
            "blocker": "review_debt:human_reviewed_task_relations",
            "decision_surface": f"reports/aihr_review_seedpack_blocker_ranked_{stamp}.csv",
            "row_selector": "issue_type=ontology_task_ksa_relation_human_review_required",
            "post_decision_command": (
                "python scripts\\audit_aihr_review_seedpack_csv_decisions.py "
                f"--csv reports\\aihr_review_seedpack_blocker_ranked_{stamp}.csv "
                f"--out reports\\aihr_review_seedpack_task_relation_decision_audit_{stamp}.json "
                f"--markdown-out reports\\aihr_review_seedpack_task_relation_decision_audit_{stamp}.md "
                "--issue-type ontology_task_ksa_relation_human_review_required "
                "--require-completed-decisions "
                "--strict"
            ),
            "post_decision_audit_key": "task_relation_seedpack_audit",
            "supplemental_commands": [],
            "notes": "Filter the shared blocker-ranked audit rows by task-KSA issue type before semantic review.",
        },
        {
            "blocker": "transition_eval:trusted_scenarios",
            "decision_surface": f"reports/transition_provenance_operator_crosswalk_{stamp}.csv",
            "actual_decision_sheet": f"reports/human_review_provenance_reconfirmation_decision_sheet_{stamp}.csv",
            "post_decision_command": (
                "python scripts\\audit_provenance_reconfirmation_decisions.py "
                f"--csv reports\\human_review_provenance_reconfirmation_decision_sheet_{stamp}.csv "
                f"--source-packet reports\\human_review_provenance_reconfirmation_packet_{stamp}.json "
                f"--out reports\\human_review_provenance_reconfirmation_decision_audit_{stamp}.json "
                f"--markdown-out reports\\human_review_provenance_reconfirmation_decision_audit_{stamp}.md"
            ),
            "post_decision_audit_key": "provenance_decision_audit",
            "supplemental_commands": [],
            "notes": "Open the crosswalk first, then validate the actual provenance decision sheet. Do not rerun the proofset exporter over a filled CSV.",
        },
        {
            "blocker": "human_review:provenance_reconfirmation_required",
            "decision_surface": f"reports/human_review_provenance_reconfirmation_decision_sheet_{stamp}.csv",
            "post_decision_command": (
                "python scripts\\audit_provenance_reconfirmation_decisions.py "
                f"--csv reports\\human_review_provenance_reconfirmation_decision_sheet_{stamp}.csv "
                f"--source-packet reports\\human_review_provenance_reconfirmation_packet_{stamp}.json "
                f"--out reports\\human_review_provenance_reconfirmation_decision_audit_{stamp}.json "
                f"--markdown-out reports\\human_review_provenance_reconfirmation_decision_audit_{stamp}.md"
            ),
            "post_decision_audit_key": "provenance_decision_audit",
            "supplemental_commands": [],
            "notes": "Audit only. A reconfirm decision remains input until a separate guarded apply is explicitly approved.",
        },
        {
            "blocker": "qualification:collection_coverage",
            "decision_surface": f"reports/qualification_guarded_batch_operator_decision_{stamp}.csv",
            "post_decision_command": (
                "python scripts\\audit_qualification_guarded_batch_operator_decision.py "
                f"--packet reports\\qualification_guarded_batch_operator_decision_{stamp}.json "
                f"--out reports\\qualification_guarded_batch_operator_decision_audit_{stamp}.json "
                f"--markdown-out reports\\qualification_guarded_batch_operator_decision_audit_{stamp}.md "
                "--strict"
            ),
            "post_decision_audit_key": "qualification_decision_audit",
            "supplemental_commands": [
                "After an operator-run collection, rerun qualification-summary, qualification-retry-hygiene, and qualification-coverage-plan with the same checkpoint path.",
            ],
            "notes": "This packet does not authorize API collection. It only records the guarded timing and post-run verification checklist.",
        },
    ]


def build_matrix(
    *,
    stamp: str,
    root: Path = PROJECT_ROOT,
    paths: dict[str, Path] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    paths = paths or default_paths(stamp, root=root)
    payloads = {key: read_json(path) for key, path in paths.items()}
    source_artifacts = {key: source_status(path, root=root) for key, path in paths.items()}
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for key in (
        "release_blocker_addendum",
        "operator_workbench",
        "ontology_seedpack_audit",
        "goal_link_seedpack_audit",
        "task_relation_seedpack_audit",
        "provenance_decision_audit",
        "qualification_decision",
        "qualification_decision_audit",
    ):
        if source_artifacts.get(key, {}).get("exists_nonempty") is not True:
            issues.append(
                issue(
                    "missing_required_source",
                    "A required post-decision validation source artifact is missing.",
                    source=key,
                    path=source_artifacts.get(key, {}).get("path"),
                )
            )

    source_contracts = {
        key: safe_contract(payload)
        for key, payload in payloads.items()
        if payload
    }
    for key, contract in source_contracts.items():
        if not bool_all(contract):
            issues.append(
                issue(
                    "unsafe_source_contract",
                    "A source artifact does not preserve report-only safety flags.",
                    source=key,
                    contract=contract,
                )
            )
        elif payloads.get(key, {}).get("report_only") is None:
            warnings.append(
                issue(
                    "legacy_report_only_field_absent",
                    "A source artifact omits top-level report_only but keeps explicit false safety flags.",
                    source=key,
                )
            )

    rows = []
    for row in validation_rows(stamp=stamp, root=root):
        audit_key = row["post_decision_audit_key"]
        audit_payload = payloads.get(audit_key) or {}
        decision_surface_path = path_from(row["decision_surface"], root=root)
        surface_status = source_status(decision_surface_path, root=root)
        actual_decision_sheet_status = None
        if row.get("actual_decision_sheet"):
            actual_decision_sheet_status = source_status(
                path_from(str(row["actual_decision_sheet"]), root=root),
                root=root,
            )
        audit_path = paths.get(audit_key)
        audit_status = source_status(audit_path, root=root)
        audit_contract = source_contracts.get(audit_key, {})
        audit = audit_summary(audit_payload)
        pending_decision_count = int_or_none(audit.get("pending_decision_count"))
        invalid_decision_count = int_or_none(audit.get("invalid_decision_count"))
        guard_issue_row_count = int_or_none(audit.get("guard_issue_row_count"))
        audit_row_count = int_or_none(audit.get("row_count"))
        workbench = workbench_scope(str(row.get("blocker") or ""), payloads.get("operator_workbench") or {})
        workbench_selected_count = int_value(workbench.get("selected_row_count"))
        scope_exceeds_workbench = bool(
            audit_row_count
            and workbench_selected_count
            and audit_row_count > workbench_selected_count
        )
        expected_completion_guard = "--require-completed-decisions" in str(
            row.get("post_decision_command") or ""
        )
        completion_guard_ok = (
            not expected_completion_guard
            or audit_payload.get("require_completed_decisions") is True
        )
        completion_pending_failure = (
            audit_payload.get("require_completed_decisions") is True
            and audit_payload.get("completion_issue") is True
            and pending_decision_count not in (None, 0)
            and invalid_decision_count in (None, 0)
            and guard_issue_row_count in (None, 0)
            and not audit_payload.get("missing_required_columns")
        )
        audit_semantic_ok = audit_payload.get("ok") is True or completion_pending_failure
        route_ok = (
            surface_status.get("exists_nonempty") is True
            and (
                actual_decision_sheet_status is None
                or actual_decision_sheet_status.get("exists_nonempty") is True
            )
            and audit_status.get("exists_nonempty") is True
            and bool_all(audit_contract)
            and completion_guard_ok
            and audit_semantic_ok
        )
        row_ok = (
            route_ok
            and audit_payload.get("ok") is True
            and (pending_decision_count is None or pending_decision_count == 0)
            and (invalid_decision_count is None or invalid_decision_count == 0)
        )
        rows.append(
            row
            | {
                "decision_surface_status": surface_status,
                "actual_decision_sheet_status": actual_decision_sheet_status,
                "audit_status": audit_status,
                "audit_contract": audit_contract,
                "audit_summary": audit,
                "workbench_scope": workbench,
                "audit_row_count": audit_row_count,
                "workbench_selected_row_count": workbench_selected_count,
                "audit_scope_exceeds_workbench_selection": scope_exceeds_workbench,
                "expected_completion_guard": expected_completion_guard,
                "completion_guard_ok": completion_guard_ok,
                "completion_pending_failure": completion_pending_failure,
                "audit_semantic_ok": audit_semantic_ok,
                "route_ok": route_ok,
                "row_ok": row_ok,
                "status_update_allowed": False,
                "db_writes": False,
                "api_calls": False,
                "approval_claim": False,
                "human_decision_required": True,
            }
        )
        if surface_status.get("exists_nonempty") is not True:
            issues.append(
                issue(
                    "missing_decision_surface",
                    "A blocker decision surface is missing or empty.",
                    blocker=row.get("blocker"),
                    path=row.get("decision_surface"),
                )
            )
        if (
            actual_decision_sheet_status is not None
            and actual_decision_sheet_status.get("exists_nonempty") is not True
        ):
            issues.append(
                issue(
                    "missing_actual_decision_sheet",
                    "A blocker actual decision sheet is missing or empty.",
                    blocker=row.get("blocker"),
                    path=row.get("actual_decision_sheet"),
                )
            )
        if audit_payload and expected_completion_guard and not completion_guard_ok:
            issues.append(
                issue(
                    "post_decision_audit_missing_completion_guard",
                    "A blocker post-decision audit artifact was not generated with require_completed_decisions.",
                    blocker=row.get("blocker"),
                    audit_key=audit_key,
                    audit_summary=audit_summary(audit_payload),
                )
            )
        if audit_payload and not audit_semantic_ok:
            issues.append(
                issue(
                    "post_decision_audit_not_ok",
                    "A blocker post-decision audit has a hard failure other than expected pending human decisions.",
                    blocker=row.get("blocker"),
                    audit_key=audit_key,
                    audit_summary=audit_summary(audit_payload),
                )
            )

    duplicate_commands = {}
    for row in rows:
        command = row.get("post_decision_command")
        duplicate_commands.setdefault(command, []).append(row.get("blocker"))
    shared_commands = {
        command: blockers
        for command, blockers in duplicate_commands.items()
        if command and len(blockers) > 1
    }
    if shared_commands:
        warnings.append(
            issue(
                "shared_post_decision_command",
                "Multiple blockers share one post-decision audit command; read row_selector before semantic review.",
                shared_commands=shared_commands,
            )
        )

    scope_gap_rows = [
        row
        for row in rows
        if row.get("audit_scope_exceeds_workbench_selection") is True
    ]
    if scope_gap_rows:
        warnings.append(
            issue(
                "post_decision_audit_scope_exceeds_workbench_selection",
                "Post-decision audit scope includes more rows than the current operator workbench sprint selection.",
                blockers=[
                    {
                        "blocker": row.get("blocker"),
                        "audit_row_count": row.get("audit_row_count"),
                        "workbench_selected_row_count": row.get("workbench_selected_row_count"),
                    }
                    for row in scope_gap_rows
                ],
            )
        )

    pending_rows = [
        row
        for row in rows
        if int_or_none((row.get("audit_summary") or {}).get("pending_decision_count"))
        not in (None, 0)
    ]
    if pending_rows:
        issues.append(
            issue(
                "post_decision_rows_pending",
                "One or more post-decision validation rows still require human decisions.",
                blockers=[row.get("blocker") for row in pending_rows],
                pending_row_count=len(pending_rows),
            )
        )

    hard_issue_count = sum(
        1 for item in issues if item.get("code") != "post_decision_rows_pending"
    )
    row_ok_count = sum(1 for row in rows if row.get("row_ok") is True)
    scope_gap_count = len(scope_gap_rows)
    summary = {
        "validation_row_count": len(rows),
        "route_ok_count": sum(1 for row in rows if row.get("route_ok") is True),
        "row_ok_count": row_ok_count,
        "pending_post_decision_row_count": sum(
            1
            for row in rows
            if int_or_none((row.get("audit_summary") or {}).get("pending_decision_count")) not in (None, 0)
        ),
        "missing_required_source_count": sum(
            1 for item in issues if item.get("code") == "missing_required_source"
        ),
        "post_decision_audit_not_ok_count": sum(
            1 for item in issues if item.get("code") == "post_decision_audit_not_ok"
        ),
        "audit_scope_exceeds_workbench_selection_count": scope_gap_count,
        "hard_issue_count": hard_issue_count,
        "pending_issue_count": sum(
            1 for item in issues if item.get("code") == "post_decision_rows_pending"
        ),
        "issue_count": len(issues),
        "warning_count": len(warnings),
    }
    status = (
        "fail"
        if hard_issue_count
        else "pass_with_workbench_scope_warnings"
        if row_ok_count == len(rows) and scope_gap_count
        else "pass"
        if row_ok_count == len(rows)
        else "pending_human_decisions"
    )
    return {
        "schema": SCHEMA,
        "generated_at": generated_at or now_iso(),
        "ok": status == "pass",
        "status": status,
        "terminal_evidence_only": True,
        "include_in_release_refresh_dag": False,
        "include_in_operator_handoff": False,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "acceptance_claim": False,
        "human_decision_required": True,
        "issue_count": len(issues),
        "warning_count": len(warnings),
        "forbidden_automatic_statuses": FORBIDDEN_AUTOMATIC_STATUSES,
        "stamp": stamp,
        "source_artifacts": source_artifacts,
        "source_contracts": source_contracts,
        "summary": summary,
        "validation_rows": rows,
        "issues": issues,
        "warnings": warnings,
        "notes": [
            "This matrix is a post-decision validation index; it does not import decisions or update DB status.",
            "Shared CSV audits must be filtered by row_selector before semantic review.",
            "Qualification rows are guarded timing evidence only and do not authorize API collection.",
        ],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "# AI-HR Post-Decision Validation Matrix",
        "",
        f"- schema: `{payload.get('schema')}`",
        f"- generated_at: `{payload.get('generated_at')}`",
        f"- status: `{payload.get('status')}`",
        f"- ok: `{payload.get('ok')}`",
        f"- terminal_evidence_only: `{payload.get('terminal_evidence_only')}`",
        f"- include_in_release_refresh_dag: `{payload.get('include_in_release_refresh_dag')}`",
        f"- include_in_operator_handoff: `{payload.get('include_in_operator_handoff')}`",
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
        "validation_row_count",
        "route_ok_count",
        "row_ok_count",
        "pending_post_decision_row_count",
        "missing_required_source_count",
        "post_decision_audit_not_ok_count",
        "audit_scope_exceeds_workbench_selection_count",
        "hard_issue_count",
        "pending_issue_count",
        "issue_count",
        "warning_count",
    ):
        lines.append(f"- {key}: `{summary.get(key)}`")
    lines.extend(["", "## Rows", ""])
    for row in payload.get("validation_rows") or []:
        audit = row.get("audit_summary") if isinstance(row.get("audit_summary"), dict) else {}
        lines.append(f"### {row.get('blocker')}")
        lines.append(f"- decision_surface: `{row.get('decision_surface')}`")
        if row.get("row_selector"):
            lines.append(f"- row_selector: `{row.get('row_selector')}`")
        if row.get("actual_decision_sheet"):
            lines.append(f"- actual_decision_sheet: `{row.get('actual_decision_sheet')}`")
        lines.append(f"- route_ok: `{row.get('route_ok')}`")
        lines.append(f"- row_ok: `{row.get('row_ok')}`")
        lines.append(f"- workbench_selected_row_count: `{row.get('workbench_selected_row_count')}`")
        lines.append(f"- audit_row_count: `{row.get('audit_row_count')}`")
        lines.append(
            "- audit_scope_exceeds_workbench_selection: "
            f"`{row.get('audit_scope_exceeds_workbench_selection')}`"
        )
        lines.append(f"- post_decision_command: `{row.get('post_decision_command')}`")
        lines.append(f"- audit_schema: `{audit.get('schema')}`")
        lines.append(f"- audit_ok: `{audit.get('ok')}`")
        lines.append(f"- pending_decision_count: `{audit.get('pending_decision_count')}`")
        lines.append(f"- completed_decision_count: `{audit.get('completed_decision_count')}`")
        lines.append(f"- invalid_decision_count: `{audit.get('invalid_decision_count')}`")
        lines.append(f"- notes: {row.get('notes')}")
        lines.append("")
    if payload.get("issues"):
        lines.extend(["## Issues", ""])
        for item in payload.get("issues") or []:
            lines.append(f"- `{item.get('code')}`: {item.get('message')}")
    else:
        lines.append("No post-decision validation matrix issues found.")
    if payload.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        for item in payload.get("warnings") or []:
            lines.append(f"- `{item.get('code')}`: {item.get('message')}")
    lines.extend(["", "## Notes", ""])
    lines.extend(f"- {note}" for note in payload.get("notes") or [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build AI-HR post-decision validation matrix.")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--stamp", default="20260712_10h")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_matrix(stamp=args.stamp, root=args.root)
    write_json(args.out, report)
    if args.markdown_out:
        write_markdown(args.markdown_out, report)
    print(
        json.dumps(
            {
                "ok": report.get("ok"),
                "schema": report.get("schema"),
                "status": report.get("status"),
                "validation_row_count": report.get("summary", {}).get("validation_row_count"),
                "row_ok_count": report.get("summary", {}).get("row_ok_count"),
                "issue_count": report.get("summary", {}).get("issue_count"),
                "warning_count": report.get("summary", {}).get("warning_count"),
                "out_path": str(args.out),
                "markdown_path": str(args.markdown_out) if args.markdown_out else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if args.strict and report.get("status") != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())
