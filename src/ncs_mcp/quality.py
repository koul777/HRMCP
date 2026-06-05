from __future__ import annotations

if __package__ in {None, ""}:  # pragma: no cover - direct script support
    import sys
    from pathlib import Path

    sys.path.append(str(Path(__file__).resolve().parents[1]))

import argparse
import json
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
        ("element", "competency_elements", "element_id", "element_name_raw"),
        ("criteria", "performance_criteria", "criteria_id", "criteria_text_raw"),
        ("ksa", "ksa_items", "ksa_id", "ksa_text_raw"),
    ]
    for target_type, table, id_col, text_col in double_space_targets:
        rows = conn.execute(
            f"SELECT {id_col} AS target_id, {text_col} AS value FROM {table} WHERE {text_col} LIKE '%  %'"
        ).fetchall()
        for row in rows:
            insert_quality_issue(
                conn,
                target_type=target_type,
                target_id=row["target_id"],
                issue_type="double_space",
                severity="info",
                issue_detail=f"이중 공백 포함: {row['value']}",
                suggested_action="정제본에서 공백을 정규화한다.",
            )
            counts["double_space"] += 1

    rows = conn.execute(
        """
        SELECT criteria_id, criteria_text_raw
        FROM performance_criteria
        WHERE criteria_text_raw NOT LIKE '%할 수 있다%'
        """
    ).fetchall()
    for row in rows:
        insert_quality_issue(
            conn,
            target_type="criteria",
            target_id=row["criteria_id"],
            issue_type="criteria_format_issue",
            severity="warning",
            issue_detail=f"수행준거 표준 표현 확인 필요: {row['criteria_text_raw']}",
            suggested_action="'할 수 있다' 수행문 형태인지 검토한다.",
        )
        counts["criteria_format_issue"] += 1

    rows = conn.execute(
        """
        SELECT criteria_id, criteria_text_raw
        FROM performance_criteria
        WHERE criteria_text_raw != '' AND SUBSTR(criteria_text_raw, -1) != '.'
        """
    ).fetchall()
    for row in rows:
        insert_quality_issue(
            conn,
            target_type="criteria",
            target_id=row["criteria_id"],
            issue_type="criteria_format_issue",
            severity="info",
            issue_detail=f"마침표 누락 가능성: {row['criteria_text_raw']}",
            suggested_action="원문 유지 후 정제본에서 문장부호 보정 여부를 검토한다.",
        )
        counts["criteria_format_issue"] += 1

    for pattern in TYPO_PATTERNS:
        for target_type, table, id_col, text_col in [
            ("element", "competency_elements", "element_id", "element_name_raw"),
            ("criteria", "performance_criteria", "criteria_id", "criteria_text_raw"),
            ("ksa", "ksa_items", "ksa_id", "ksa_text_raw"),
        ]:
            rows = conn.execute(
                f"SELECT {id_col} AS target_id, {text_col} AS value FROM {table} WHERE {text_col} LIKE ?",
                (f"%{pattern}%",),
            ).fetchall()
            for row in rows:
                insert_quality_issue(
                    conn,
                    target_type=target_type,
                    target_id=row["target_id"],
                    issue_type="suspected_typo",
                    severity="warning",
                    issue_detail=f"의심 문자열 '{pattern}' 포함: {row['value']}",
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

