from __future__ import annotations

if __package__ in {None, ""}:  # pragma: no cover - direct script support
    import sys
    from pathlib import Path

    sys.path.append(str(Path(__file__).resolve().parents[1]))

import argparse
import json
import re
from pathlib import Path

from ncs_mcp.config import load_settings
from ncs_mcp.db import (
    clear_quality_issues,
    connect,
    initialize_database,
    insert_quality_issue,
    now_utc,
)


RULE_ISSUE_TYPES = [
    "missing_required_value",
    "duplicate_text",
    "short_ksa",
    "double_space",
    "suspected_typo",
    "criteria_format_issue",
]

TYPO_PATTERNS = ["자가가", "조진", "요견"]
TYPO_FALSE_POSITIVE_SUBSTRINGS = [
    "자가가구",
    "보조진행자",
    "구조진동해석",
]
CRITERIA_STANDARD_ENDINGS = (
    "수 있다",
    "수있다",
    "수 있어야 한다",
    "수있어야 한다",
    "알고 있다",
    "인지하고 있다",
    "파악하고 있다",
    "이해하고 있다",
    "한다",
    "된다",
    "받는다",
    "있어야 한다",
    "하여야 한다",
)


def is_typo_false_positive(text: str | None) -> bool:
    value = text or ""
    return any(allowed in value for allowed in TYPO_FALSE_POSITIVE_SUBSTRINGS)


def effective_text(raw_text: str | None, refined_text: str | None = None) -> str:
    refined = (refined_text or "").strip()
    if refined:
        return refined
    return raw_text or ""


def is_standard_criteria_expression(text: str | None) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip().rstrip(".")
    if not normalized:
        return False
    return any(normalized.endswith(ending) for ending in CRITERIA_STANDARD_ENDINGS)


def run_quality_checks(db_path: Path, reports_dir: Path) -> dict[str, int]:
    conn = connect(db_path)
    initialize_database(conn)
    clear_quality_issues(conn, RULE_ISSUE_TYPES)

    counts: dict[str, int] = {issue_type: 0 for issue_type in RULE_ISSUE_TYPES}

    for table, target_type, id_col, fields in [
        (
            "competency_units",
            "unit",
            "unit_code",
            ["unit_code", "unit_name_raw", "unit_level_raw"],
        ),
        (
            "competency_elements",
            "element",
            "element_id",
            ["element_code_raw", "element_name_raw", "element_level_raw"],
        ),
        (
            "performance_criteria",
            "criteria",
            "criteria_id",
            ["criteria_no", "criteria_text_raw"],
        ),
        ("ksa_items", "ksa", "ksa_id", ["ksa_type_name", "ksa_no", "ksa_text_raw"]),
    ]:
        for field in fields:
            rows = conn.execute(
                f"SELECT {id_col} AS target_id FROM {table} WHERE {field} IS NULL OR TRIM({field}) = ''"
            ).fetchall()
            for row in rows:
                insert_quality_issue(
                    conn,
                    target_type=target_type,
                    target_id=row["target_id"],
                    issue_type="missing_required_value",
                    severity="error",
                    issue_detail=f"{field} 필수값이 비어 있다.",
                    suggested_action="원본 엑셀 값을 확인한다.",
                )
                counts["missing_required_value"] += 1

    rows = conn.execute(
        """
        SELECT ksa_id, ksa_text_raw
        FROM ksa_items
        WHERE LENGTH(REPLACE(ksa_text_raw, ' ', '')) <= 6
        """
    ).fetchall()
    for row in rows:
        insert_quality_issue(
            conn,
            target_type="ksa",
            target_id=row["ksa_id"],
            issue_type="short_ksa",
            severity="info",
            issue_detail=f"짧은 KSA: {row['ksa_text_raw']}",
            suggested_action="맥락 보강 또는 정제 필요 여부를 검토한다.",
        )
        counts["short_ksa"] += 1

    duplicate_texts = conn.execute(
        """
        SELECT ksa_text_raw
        FROM ksa_items
        GROUP BY ksa_text_raw
        HAVING COUNT(DISTINCT element_id) >= 4
        """
    ).fetchall()
    for text_row in duplicate_texts:
        rows = conn.execute(
            "SELECT ksa_id FROM ksa_items WHERE ksa_text_raw = ?",
            (text_row["ksa_text_raw"],),
        ).fetchall()
        for row in rows:
            insert_quality_issue(
                conn,
                target_type="ksa",
                target_id=row["ksa_id"],
                issue_type="duplicate_text",
                severity="info",
                issue_detail=f"여러 능력단위요소에 반복되는 KSA: {text_row['ksa_text_raw']}",
                suggested_action="반복이 의도된 공통 지식인지 확인한다.",
            )
            counts["duplicate_text"] += 1

    double_space_targets = [
        ("element", "competency_elements", "element_id", "element_name_raw", "element_name_refined"),
        ("criteria", "performance_criteria", "criteria_id", "criteria_text_raw", "criteria_text_refined"),
        ("ksa", "ksa_items", "ksa_id", "ksa_text_raw", "ksa_text_refined"),
    ]
    for target_type, table, id_col, text_col, refined_col in double_space_targets:
        rows = conn.execute(
            f"""
            SELECT {id_col} AS target_id, {text_col} AS raw_value, {refined_col} AS refined_value
            FROM {table}
            WHERE {text_col} LIKE '%  %' OR {refined_col} LIKE '%  %'
            """
        ).fetchall()
        for row in rows:
            value = effective_text(row["raw_value"], row["refined_value"])
            if "  " not in value:
                continue
            insert_quality_issue(
                conn,
                target_type=target_type,
                target_id=row["target_id"],
                issue_type="double_space",
                severity="info",
                issue_detail=f"이중 공백 포함: {value}",
                suggested_action="정제본에서 공백을 정규화한다.",
            )
            counts["double_space"] += 1

    rows = conn.execute(
        """
        SELECT criteria_id, criteria_text_raw, criteria_text_refined
        FROM performance_criteria
        """
    ).fetchall()
    for row in rows:
        value = effective_text(row["criteria_text_raw"], row["criteria_text_refined"])
        if is_standard_criteria_expression(value):
            continue
        insert_quality_issue(
            conn,
            target_type="criteria",
            target_id=row["criteria_id"],
            issue_type="criteria_format_issue",
            severity="warning",
            issue_detail=f"수행준거 표준 표현 확인 필요: {value}",
            suggested_action="'할 수 있다' 수행문 형태인지 검토한다.",
        )
        counts["criteria_format_issue"] += 1

    rows = conn.execute(
        """
        SELECT criteria_id, criteria_text_raw, criteria_text_refined
        FROM performance_criteria
        WHERE criteria_text_raw != ''
        """
    ).fetchall()
    for row in rows:
        value = effective_text(row["criteria_text_raw"], row["criteria_text_refined"])
        if not value or value[-1] == ".":
            continue
        insert_quality_issue(
            conn,
            target_type="criteria",
            target_id=row["criteria_id"],
            issue_type="criteria_format_issue",
            severity="info",
            issue_detail=f"마침표 누락 가능성: {value}",
            suggested_action="원문 유지 후 정제본에서 문장부호 보정 여부를 검토한다.",
        )
        counts["criteria_format_issue"] += 1

    for pattern in TYPO_PATTERNS:
        for target_type, table, id_col, text_col, refined_col in [
            ("element", "competency_elements", "element_id", "element_name_raw", "element_name_refined"),
            ("criteria", "performance_criteria", "criteria_id", "criteria_text_raw", "criteria_text_refined"),
            ("ksa", "ksa_items", "ksa_id", "ksa_text_raw", "ksa_text_refined"),
        ]:
            rows = conn.execute(
                f"""
                SELECT {id_col} AS target_id, {text_col} AS raw_value, {refined_col} AS refined_value
                FROM {table}
                WHERE {text_col} LIKE ? OR {refined_col} LIKE ?
                """,
                (f"%{pattern}%", f"%{pattern}%"),
            ).fetchall()
            for row in rows:
                value = effective_text(row["raw_value"], row["refined_value"])
                if pattern not in value or is_typo_false_positive(value):
                    continue
                insert_quality_issue(
                    conn,
                    target_type=target_type,
                    target_id=row["target_id"],
                    issue_type="suspected_typo",
                    severity="warning",
                    issue_detail=f"의심 문자열 '{pattern}' 포함: {value}",
                    suggested_action="원문과 정제본을 병행 보관하고 수동 검토한다.",
                )
                counts["suspected_typo"] += 1

    conn.commit()
    write_report(conn, counts, reports_dir)
    conn.close()
    return counts


def write_report(conn, counts: dict[str, int], reports_dir: Path) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    top_rows = conn.execute(
        """
        SELECT issue_type, severity, COUNT(*) AS count
        FROM quality_issues
        GROUP BY issue_type, severity
        ORDER BY count DESC
        """
    ).fetchall()
    sample_rows = conn.execute(
        """
        SELECT issue_type, target_type, target_id, severity, issue_detail
        FROM quality_issues
        ORDER BY issue_id
        LIMIT 100
        """
    ).fetchall()
    lines = [
        "# NCS 품질 진단 리포트",
        "",
        f"- 생성시각: {now_utc()}",
        "",
        "## 신규 규칙별 탐지 건수",
        "",
        "| 이슈 유형 | 건수 |",
        "|---|---:|",
    ]
    for issue_type, count in counts.items():
        lines.append(f"| `{issue_type}` | {count:,} |")
    lines.extend(["", "## 전체 품질 이슈 집계", "", "| 이슈 유형 | 심각도 | 건수 |", "|---|---|---:|"])
    for row in top_rows:
        lines.append(f"| `{row['issue_type']}` | {row['severity']} | {row['count']:,} |")
    lines.extend(["", "## 샘플 100건", "", "| 유형 | 대상 | ID | 심각도 | 내용 |", "|---|---|---:|---|---|"])
    for row in sample_rows:
        detail = str(row["issue_detail"]).replace("|", "\\|")
        lines.append(
            f"| `{row['issue_type']}` | {row['target_type']} | {row['target_id']} | {row['severity']} | {detail} |"
        )
    (reports_dir / "quality_issues.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (reports_dir / "quality_issues.json").write_text(
        json.dumps(counts, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Run rule-based NCS data quality checks.")
    parser.add_argument("--db-path", type=Path, default=settings.db_path)
    parser.add_argument("--reports-dir", type=Path, default=settings.reports_dir)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    counts = run_quality_checks(args.db_path, args.reports_dir)
    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
