from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "processed" / "ncs.db"
DEFAULT_OUT = PROJECT_ROOT / "reports" / "review_status_recent_write_audit_20260620.json"
DEFAULT_MARKDOWN_OUT = PROJECT_ROOT / "reports" / "review_status_recent_write_audit_20260620.md"

TRUSTED_STATUS_VALUES = ("accepted", "human_reviewed", "reviewed")
MONITORED_NON_TRUSTED_STATUS_VALUES = ("candidate_auto",)
AUDITED_STATUS_VALUES = TRUSTED_STATUS_VALUES + MONITORED_NON_TRUSTED_STATUS_VALUES
STATUS_COLUMNS = ("review_status", "link_status")
TIME_COLUMNS = ("updated_at", "created_at", "reviewed_at")
STATUS_CHANGE_TIME_COLUMNS = ("reviewed_at", "created_at")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_second(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def open_readonly_connection(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row["name"])
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})")]


def table_primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    columns = []
    for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})"):
        if int(row["pk"] or 0) > 0:
            columns.append(str(row["name"]))
    return columns


def row_identity(row: sqlite3.Row, pk_columns: list[str]) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for column in pk_columns:
        if column in row.keys():
            identity[column] = row[column]
    if not identity and "_rowid" in row.keys():
        identity["rowid"] = row["_rowid"]
    return identity


def audit_status_table(
    conn: sqlite3.Connection,
    *,
    table: str,
    status_column: str,
    cutoff: datetime,
    sample_limit: int,
) -> dict[str, Any]:
    columns = table_columns(conn, table)
    time_columns = [column for column in TIME_COLUMNS if column in columns]
    pk_columns = table_primary_key_columns(conn, table)
    select_columns = [status_column, *time_columns, *pk_columns]
    if not pk_columns:
        select_sql = "rowid AS _rowid, " + ", ".join(quote_ident(column) for column in select_columns)
    else:
        select_sql = ", ".join(quote_ident(column) for column in select_columns)
    placeholders = ", ".join("?" for _ in AUDITED_STATUS_VALUES)
    sql = (
        f"SELECT {select_sql} FROM {quote_ident(table)} "
        f"WHERE {quote_ident(status_column)} IN ({placeholders})"
    )
    rows = list(conn.execute(sql, AUDITED_STATUS_VALUES))
    recent_samples: list[dict[str, Any]] = []
    recent_monitored_non_trusted_samples: list[dict[str, Any]] = []
    recent_unverifiable_samples: list[dict[str, Any]] = []
    recent_monitored_non_trusted_unverifiable_samples: list[dict[str, Any]] = []
    invalid_timestamp_count = 0
    recent_count = 0
    recent_monitored_non_trusted_count = 0
    recent_unverifiable_count = 0
    recent_monitored_non_trusted_unverifiable_count = 0
    trusted_status_count = 0
    monitored_non_trusted_status_count = 0
    for row in rows:
        status_value = str(row[status_column] or "")
        is_trusted = status_value in TRUSTED_STATUS_VALUES
        is_monitored_non_trusted = status_value in MONITORED_NON_TRUSTED_STATUS_VALUES
        if is_trusted:
            trusted_status_count += 1
        elif is_monitored_non_trusted:
            monitored_non_trusted_status_count += 1
        parsed_times: list[tuple[str, datetime]] = []
        for column in time_columns:
            raw_value = row[column]
            if raw_value in (None, ""):
                continue
            parsed = parse_iso_datetime(raw_value)
            if parsed is None:
                invalid_timestamp_count += 1
                continue
            parsed_times.append((column, parsed))
        if parsed_times:
            status_change_times = [
                item for item in parsed_times if item[0] in STATUS_CHANGE_TIME_COLUMNS
            ]
            generic_times = [
                item for item in parsed_times if item[0] not in STATUS_CHANGE_TIME_COLUMNS
            ]
            latest_status_column, latest_status_time = max(
                status_change_times,
                key=lambda item: item[1],
                default=(None, None),
            )
            latest_generic_column, latest_generic_time = max(
                generic_times,
                key=lambda item: item[1],
                default=(None, None),
            )
            if latest_status_time is not None and latest_status_time >= cutoff:
                sample = {
                    "table": table,
                    "status_column": status_column,
                    "status": row[status_column],
                    "latest_time_column": latest_status_column,
                    "latest_time": isoformat_second(latest_status_time),
                    "identity": row_identity(row, pk_columns),
                }
                if is_trusted:
                    recent_count += 1
                    if len(recent_samples) < sample_limit:
                        recent_samples.append(sample)
                elif is_monitored_non_trusted:
                    recent_monitored_non_trusted_count += 1
                    if len(recent_monitored_non_trusted_samples) < sample_limit:
                        recent_monitored_non_trusted_samples.append(sample)
            elif latest_generic_time is not None and latest_generic_time >= cutoff:
                sample = {
                    "table": table,
                    "status_column": status_column,
                    "status": row[status_column],
                    "latest_time_column": latest_generic_column,
                    "latest_time": isoformat_second(latest_generic_time),
                    "identity": row_identity(row, pk_columns),
                    "reason": (
                        "recent_generic_timestamp_on_audited_status_row_without_recent_"
                        "status_change_timestamp"
                    ),
                }
                if is_trusted:
                    recent_unverifiable_count += 1
                    if len(recent_unverifiable_samples) < sample_limit:
                        recent_unverifiable_samples.append(sample)
                elif is_monitored_non_trusted:
                    recent_monitored_non_trusted_unverifiable_count += 1
                    if len(recent_monitored_non_trusted_unverifiable_samples) < sample_limit:
                        recent_monitored_non_trusted_unverifiable_samples.append(sample)
    return {
        "table": table,
        "status_column": status_column,
        "trusted_status_count": trusted_status_count,
        "monitored_non_trusted_status_count": monitored_non_trusted_status_count,
        "time_columns": time_columns,
        "recent_trusted_status_count": recent_count,
        "recent_monitored_non_trusted_status_count": recent_monitored_non_trusted_count,
        "recent_unverifiable_generic_timestamp_count": recent_unverifiable_count,
        "recent_monitored_non_trusted_unverifiable_generic_timestamp_count": (
            recent_monitored_non_trusted_unverifiable_count
        ),
        "unverifiable_no_timestamp": trusted_status_count > 0 and not time_columns,
        "monitored_non_trusted_unverifiable_no_timestamp": (
            monitored_non_trusted_status_count > 0 and not time_columns
        ),
        "invalid_timestamp_count": invalid_timestamp_count,
        "sample_recent_rows": recent_samples,
        "sample_recent_monitored_non_trusted_rows": recent_monitored_non_trusted_samples,
        "sample_recent_unverifiable_rows": recent_unverifiable_samples,
        "sample_recent_monitored_non_trusted_unverifiable_rows": (
            recent_monitored_non_trusted_unverifiable_samples
        ),
    }


def audit_review_audit_log(
    conn: sqlite3.Connection,
    *,
    cutoff: datetime,
    sample_limit: int,
) -> dict[str, Any]:
    tables = set(table_names(conn))
    if "review_audit_log" not in tables:
        return {
            "exists": False,
            "has_created_at": False,
            "recent_audit_log_total_count": 0,
            "recent_trusted_audit_log_count": 0,
            "recent_monitored_non_trusted_audit_log_count": 0,
            "recent_audit_rows": [],
            "recent_audit_trusted_rows": [],
            "recent_audit_monitored_non_trusted_rows": [],
        }
    columns = set(table_columns(conn, "review_audit_log"))
    if "created_at" not in columns:
        return {
            "exists": True,
            "has_created_at": False,
            "recent_audit_log_total_count": 0,
            "recent_trusted_audit_log_count": 0,
            "recent_monitored_non_trusted_audit_log_count": 0,
            "recent_audit_rows": [],
            "recent_audit_trusted_rows": [],
            "recent_audit_monitored_non_trusted_rows": [],
        }

    rows = list(
        conn.execute(
            """
            SELECT *
            FROM review_audit_log
            ORDER BY created_at DESC, id DESC
            """
        )
    )
    recent_rows: list[dict[str, Any]] = []
    trusted_rows: list[dict[str, Any]] = []
    monitored_non_trusted_rows: list[dict[str, Any]] = []

    def row_status_values(row: sqlite3.Row) -> set[str]:
        values: set[str] = set()
        for column in ("previous_status", "new_status"):
            if column in row.keys():
                text = str(row[column] or "").strip()
                if text:
                    values.add(text)
        return values

    def row_has_status(row: sqlite3.Row, statuses: tuple[str, ...]) -> bool:
        return bool(row_status_values(row).intersection(statuses))

    def row_is_recent(row: sqlite3.Row) -> bool:
        return (parse_iso_datetime(row["created_at"]) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff

    for row in rows:
        created_at = parse_iso_datetime(row["created_at"])
        if created_at is None or created_at < cutoff:
            continue
        rendered = {
            "id": row["id"] if "id" in row.keys() else None,
            "entity_type": row["entity_type"] if "entity_type" in row.keys() else None,
            "entity_id": row["entity_id"] if "entity_id" in row.keys() else None,
            "action": row["action"] if "action" in row.keys() else None,
            "previous_status": row["previous_status"] if "previous_status" in row.keys() else None,
            "new_status": row["new_status"] if "new_status" in row.keys() else None,
            "created_at": isoformat_second(created_at),
            "created_by_tool": row["created_by_tool"] if "created_by_tool" in row.keys() else None,
            "run_artifact": row["run_artifact"] if "run_artifact" in row.keys() else None,
        }
        if len(recent_rows) < sample_limit:
            recent_rows.append(rendered)
        if row_has_status(row, TRUSTED_STATUS_VALUES):
            if len(trusted_rows) < sample_limit:
                trusted_rows.append(rendered)
        if row_has_status(row, MONITORED_NON_TRUSTED_STATUS_VALUES):
            if len(monitored_non_trusted_rows) < sample_limit:
                monitored_non_trusted_rows.append(rendered)
    return {
        "exists": True,
        "has_created_at": True,
        "recent_audit_log_total_count": sum(1 for row in rows if row_is_recent(row)),
        "recent_trusted_audit_log_count": sum(
            1
            for row in rows
            if row_is_recent(row) and row_has_status(row, TRUSTED_STATUS_VALUES)
        ),
        "recent_monitored_non_trusted_audit_log_count": sum(
            1
            for row in rows
            if row_is_recent(row) and row_has_status(row, MONITORED_NON_TRUSTED_STATUS_VALUES)
        ),
        "recent_audit_rows": recent_rows,
        "recent_audit_trusted_rows": trusted_rows,
        "recent_audit_monitored_non_trusted_rows": monitored_non_trusted_rows,
    }


def build_audit(db_path: Path, cutoff: datetime, sample_limit: int = 20) -> dict[str, Any]:
    conn = open_readonly_connection(db_path)
    conn.row_factory = sqlite3.Row
    try:
        audits: list[dict[str, Any]] = []
        for table in table_names(conn):
            columns = table_columns(conn, table)
            for status_column in STATUS_COLUMNS:
                if status_column in columns:
                    audits.append(
                        audit_status_table(
                            conn,
                            table=table,
                            status_column=status_column,
                            cutoff=cutoff,
                            sample_limit=sample_limit,
                        )
                    )
        audit_log = audit_review_audit_log(conn, cutoff=cutoff, sample_limit=sample_limit)
    finally:
        conn.close()

    recent_hits = [
        sample
        for audit in audits
        for sample in audit.get("sample_recent_rows", [])
    ]
    recent_unverifiable_hits = [
        sample
        for audit in audits
        for sample in audit.get("sample_recent_unverifiable_rows", [])
    ]
    recent_monitored_non_trusted_hits = [
        sample
        for audit in audits
        for sample in audit.get("sample_recent_monitored_non_trusted_rows", [])
    ]
    recent_monitored_non_trusted_unverifiable_hits = [
        sample
        for audit in audits
        for sample in audit.get("sample_recent_monitored_non_trusted_unverifiable_rows", [])
    ]
    recent_trusted_status_table_hit_count = sum(
        int(audit.get("recent_trusted_status_count") or 0)
        for audit in audits
    )
    recent_monitored_non_trusted_status_table_hit_count = sum(
        int(audit.get("recent_monitored_non_trusted_status_count") or 0)
        for audit in audits
    )
    recent_unverifiable_generic_timestamp_count = sum(
        int(audit.get("recent_unverifiable_generic_timestamp_count") or 0)
        for audit in audits
    )
    recent_monitored_non_trusted_unverifiable_generic_timestamp_count = sum(
        int(audit.get("recent_monitored_non_trusted_unverifiable_generic_timestamp_count") or 0)
        for audit in audits
    )
    unverifiable_no_timestamp = [
        {
            "table": audit["table"],
            "status_column": audit["status_column"],
            "trusted_status_count": audit["trusted_status_count"],
        }
        for audit in audits
        if audit.get("unverifiable_no_timestamp")
    ]
    monitored_non_trusted_unverifiable_no_timestamp = [
        {
            "table": audit["table"],
            "status_column": audit["status_column"],
            "monitored_non_trusted_status_count": audit["monitored_non_trusted_status_count"],
        }
        for audit in audits
        if audit.get("monitored_non_trusted_unverifiable_no_timestamp")
    ]
    invalid_timestamp_table_row_count = sum(int(audit.get("invalid_timestamp_count") or 0) for audit in audits)
    review_audit_log_exists = bool(audit_log.get("exists"))
    review_audit_log_has_created_at = bool(audit_log.get("has_created_at"))
    ok = (
        review_audit_log_exists
        and review_audit_log_has_created_at
        and recent_trusted_status_table_hit_count == 0
        and int(audit_log.get("recent_trusted_audit_log_count") or 0) == 0
        and recent_monitored_non_trusted_status_table_hit_count == 0
        and int(audit_log.get("recent_monitored_non_trusted_audit_log_count") or 0) == 0
        and recent_unverifiable_generic_timestamp_count == 0
        and recent_monitored_non_trusted_unverifiable_generic_timestamp_count == 0
        and invalid_timestamp_table_row_count == 0
        and not unverifiable_no_timestamp
        and not monitored_non_trusted_unverifiable_no_timestamp
    )
    try:
        rendered_db_path = str(db_path.relative_to(PROJECT_ROOT))
    except ValueError:
        rendered_db_path = str(db_path)
    return {
        "schema": "aihr_recent_review_status_write_audit_v1",
        "generated_at": isoformat_second(now_utc()),
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_decision_required": True,
        "db_path": rendered_db_path,
        "read_only": True,
        "cutoff": isoformat_second(cutoff),
        "trusted_status_values": list(TRUSTED_STATUS_VALUES),
        "monitored_non_trusted_status_values": list(MONITORED_NON_TRUSTED_STATUS_VALUES),
        "audited_status_values": list(AUDITED_STATUS_VALUES),
        "ok": ok,
        "review_audit_log_exists": review_audit_log_exists,
        "review_audit_log_has_created_at": review_audit_log_has_created_at,
        "recent_trusted_status_table_hit_count": recent_trusted_status_table_hit_count,
        "recent_trusted_audit_log_count": int(audit_log.get("recent_trusted_audit_log_count") or 0),
        "recent_monitored_non_trusted_status_table_hit_count": (
            recent_monitored_non_trusted_status_table_hit_count
        ),
        "recent_monitored_non_trusted_audit_log_count": int(
            audit_log.get("recent_monitored_non_trusted_audit_log_count") or 0
        ),
        "recent_audit_log_total_count": int(audit_log.get("recent_audit_log_total_count") or 0),
        "recent_unverifiable_generic_timestamp_count": recent_unverifiable_generic_timestamp_count,
        "recent_monitored_non_trusted_unverifiable_generic_timestamp_count": (
            recent_monitored_non_trusted_unverifiable_generic_timestamp_count
        ),
        "unverifiable_no_timestamp_table_count": len(unverifiable_no_timestamp),
        "monitored_non_trusted_unverifiable_no_timestamp_table_count": (
            len(monitored_non_trusted_unverifiable_no_timestamp)
        ),
        "invalid_timestamp_table_row_count": invalid_timestamp_table_row_count,
        "recent_hits": recent_hits[:sample_limit],
        "recent_monitored_non_trusted_hits": recent_monitored_non_trusted_hits[:sample_limit],
        "recent_unverifiable_hits": recent_unverifiable_hits[:sample_limit],
        "recent_monitored_non_trusted_unverifiable_hits": (
            recent_monitored_non_trusted_unverifiable_hits[:sample_limit]
        ),
        "recent_audit_trusted_rows": audit_log.get("recent_audit_trusted_rows") or [],
        "recent_audit_monitored_non_trusted_rows": (
            audit_log.get("recent_audit_monitored_non_trusted_rows") or []
        ),
        "recent_audit_rows": audit_log.get("recent_audit_rows") or [],
        "no_timestamp_trusted": unverifiable_no_timestamp,
        "no_timestamp_monitored_non_trusted": monitored_non_trusted_unverifiable_no_timestamp,
        "table_audits": audits,
        "interpretation": (
            "ok=true means no audited table rows or audit-log entries show recent trusted "
            "review statuses or monitored automated candidate statuses, no unverifiable "
            "timestamp rows are present for those statuses, and no invalid timestamps were "
            "found. It also requires review_audit_log with created_at so delete-only "
            "candidate_auto activity remains auditable. Audit-log status checks inspect both "
            "previous_status and new_status. "
            "This is a read-only audit and does not approve any row."
        ),
    }


def write_markdown(report: dict[str, Any], out_path: Path) -> None:
    lines = [
        "# Recent Review Status Write Audit",
        "",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- cutoff: `{report.get('cutoff')}`",
        f"- ok: `{report.get('ok')}`",
        f"- report_only: `{report.get('report_only')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        f"- read_only: `{report.get('read_only')}`",
        f"- review_audit_log_exists: `{report.get('review_audit_log_exists')}`",
        f"- review_audit_log_has_created_at: `{report.get('review_audit_log_has_created_at')}`",
        f"- recent_trusted_status_table_hit_count: `{report.get('recent_trusted_status_table_hit_count')}`",
        f"- recent_trusted_audit_log_count: `{report.get('recent_trusted_audit_log_count')}`",
        "- recent_monitored_non_trusted_status_table_hit_count: "
        f"`{report.get('recent_monitored_non_trusted_status_table_hit_count')}`",
        "- recent_monitored_non_trusted_audit_log_count: "
        f"`{report.get('recent_monitored_non_trusted_audit_log_count')}`",
        f"- recent_audit_log_total_count: `{report.get('recent_audit_log_total_count')}`",
        f"- recent_unverifiable_generic_timestamp_count: `{report.get('recent_unverifiable_generic_timestamp_count')}`",
        "- recent_monitored_non_trusted_unverifiable_generic_timestamp_count: "
        f"`{report.get('recent_monitored_non_trusted_unverifiable_generic_timestamp_count')}`",
        f"- unverifiable_no_timestamp_table_count: `{report.get('unverifiable_no_timestamp_table_count')}`",
        "- monitored_non_trusted_unverifiable_no_timestamp_table_count: "
        f"`{report.get('monitored_non_trusted_unverifiable_no_timestamp_table_count')}`",
        f"- invalid_timestamp_table_row_count: `{report.get('invalid_timestamp_table_row_count')}`",
        "",
        "## Recent Trusted Table Hits",
    ]
    hits = report.get("recent_hits") if isinstance(report.get("recent_hits"), list) else []
    if not hits:
        lines.append("- None")
    else:
        lines.append("| Table | Status Column | Status | Latest Time | Identity |")
        lines.append("|---|---|---|---|---|")
        for hit in hits:
            lines.append(
                "| {table} | {status_column} | {status} | {latest_time} | `{identity}` |".format(
                    table=hit.get("table"),
                    status_column=hit.get("status_column"),
                    status=hit.get("status"),
                    latest_time=hit.get("latest_time"),
                    identity=json.dumps(hit.get("identity") or {}, ensure_ascii=False, sort_keys=True),
                )
            )
    lines.extend(["", "## Recent Monitored Non-Trusted Table Hits"])
    monitored_hits = (
        report.get("recent_monitored_non_trusted_hits")
        if isinstance(report.get("recent_monitored_non_trusted_hits"), list)
        else []
    )
    if not monitored_hits:
        lines.append("- None")
    else:
        lines.append("| Table | Status Column | Status | Latest Time | Identity |")
        lines.append("|---|---|---|---|---|")
        for hit in monitored_hits:
            lines.append(
                "| {table} | {status_column} | {status} | {latest_time} | `{identity}` |".format(
                    table=hit.get("table"),
                    status_column=hit.get("status_column"),
                    status=hit.get("status"),
                    latest_time=hit.get("latest_time"),
                    identity=json.dumps(hit.get("identity") or {}, ensure_ascii=False, sort_keys=True),
                )
            )
    lines.extend(["", "## Recent Unverifiable Generic Timestamp Hits"])
    unverifiable_hits = (
        report.get("recent_unverifiable_hits")
        if isinstance(report.get("recent_unverifiable_hits"), list)
        else []
    )
    if not unverifiable_hits:
        lines.append("- None")
    else:
        lines.append("| Table | Status Column | Status | Latest Time | Reason | Identity |")
        lines.append("|---|---|---|---|---|---|")
        for hit in unverifiable_hits:
            lines.append(
                "| {table} | {status_column} | {status} | {latest_time} | {reason} | `{identity}` |".format(
                    table=hit.get("table"),
                    status_column=hit.get("status_column"),
                    status=hit.get("status"),
                    latest_time=hit.get("latest_time"),
                    reason=hit.get("reason"),
                    identity=json.dumps(hit.get("identity") or {}, ensure_ascii=False, sort_keys=True),
                )
            )
    lines.extend(["", "## Policy", "- This audit is read-only.", "- It does not set `human_reviewed`, `reviewed`, or `accepted`."])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit recent trusted review-status writes without mutating the DB.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--since")
    parser.add_argument("--since-minutes", type=int, default=90)
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.since:
        cutoff = parse_iso_datetime(args.since)
        if cutoff is None:
            raise SystemExit(f"Invalid --since datetime: {args.since}")
    else:
        cutoff = now_utc() - timedelta(minutes=args.since_minutes)
    report = build_audit(args.db_path, cutoff, sample_limit=args.sample_limit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(report, args.markdown_out)
    report["out_path"] = str(args.out)
    report["markdown_path"] = str(args.markdown_out)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
