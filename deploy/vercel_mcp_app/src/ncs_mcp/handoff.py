from __future__ import annotations

import json
import os
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from ncs_mcp.db import INDEX_SQL, SCHEMA_SQL, connect, initialize_database, now_utc


HANDOFF_DB_NAME = "ncs_sqf.sqlite"

TABLE_NOTES: dict[str, str] = {
    "raw_excel_rows": "NCS Excel 원행. 반복 구조와 원천 행 위치를 보존한다.",
    "classifications": "NCS 대/중/소/세분류와 API 직무정의 보강값.",
    "competency_units": "NCS 능력단위. Excel 원문과 NCS005 API 보강값을 함께 둔다.",
    "competency_elements": "NCS 능력단위요소. 원문 요소와 NCS006 API 검증 상태를 둔다.",
    "performance_criteria": "능력단위요소별 수행준거.",
    "ksa_items": "능력단위요소별 지식/기술/태도. 수행준거 하위가 아니라 요소 하위 병렬 구조다.",
    "element_criteria_ksa_links": "Excel 원행의 수행준거-KSA 조합 추적 링크.",
    "api_raw_responses": "NCS API 원천 응답 캐시.",
    "api_competency_units": "NCS005 API 능력단위 마스터 캐시.",
    "sqf_duties": "SQF /openapi26 직무수준 원천 응답 정규화 테이블.",
    "sqf_ncs_matches": "SQF 직무수준과 NCS 능력단위 사이의 후보 매핑 객체.",
    "quality_issues": "전처리/조인/품질 진단 이슈.",
    "refinement_jobs": "수작업/모델 보정 작업 로그.",
    "schema_metadata": "스키마 버전 등 메타데이터.",
}

SAMPLE_QUERIES_SQL = """-- NCS-SQF handoff sample queries

-- 1. 특정 NCS 능력단위의 요소/수행준거/KSA 구조
-- :unit_code 값을 실제 능력단위 코드로 바꿔 실행한다.
SELECT
  u.unit_code,
  u.unit_name_raw AS unit_name,
  u.unit_level_raw AS unit_level,
  e.element_no,
  e.element_name_raw AS element_name,
  pc.criteria_no,
  pc.criteria_text_raw AS criterion_text,
  ki.ksa_type_name,
  ki.ksa_no,
  ki.ksa_text_raw AS ksa_text
FROM competency_units u
LEFT JOIN competency_elements e ON e.unit_code = u.unit_code
LEFT JOIN performance_criteria pc ON pc.element_id = e.element_id
LEFT JOIN ksa_items ki ON ki.element_id = e.element_id
WHERE u.unit_code = :unit_code
ORDER BY
  CAST(e.element_no AS INTEGER),
  CAST(pc.criteria_no AS INTEGER),
  ki.ksa_type_code,
  CAST(ki.ksa_no AS INTEGER);

-- 2. 특정 SQF 직무수준의 직접 근거와 후보 NCS 매핑
-- :sqf_source_key 값을 sqf_duties.source_key로 바꿔 실행한다.
SELECT
  sd.source_key,
  sd.ncs_lclas_name,
  sd.sqf_field_name,
  sd.job_name,
  sd.duty_name,
  sd.duty_level,
  sd.duty_definition,
  sd.duty_education_training,
  sd.duty_qualification,
  sd.duty_career,
  m.relation,
  m.score,
  m.confidence,
  m.review_status,
  cu.unit_code,
  cu.unit_name_raw AS unit_name,
  cu.unit_level_raw AS unit_level,
  m.evidence_text
FROM sqf_duties sd
LEFT JOIN sqf_ncs_matches m ON m.source_id = sd.source_key
LEFT JOIN competency_units cu ON cu.unit_code = m.target_id
WHERE sd.source_key = :sqf_source_key
ORDER BY m.score DESC, cu.unit_code;

-- 3. 경영지원 MVP의 SQF 직무수준과 후보 매핑 수
SELECT
  sd.source_key,
  sd.job_name,
  sd.duty_name,
  sd.duty_level,
  COUNT(m.match_id) AS mapping_candidates,
  SUM(CASE WHEN m.review_status IN ('human_reviewed', 'reviewed') THEN 1 ELSE 0 END)
    AS reviewed_mappings
FROM sqf_duties sd
LEFT JOIN sqf_ncs_matches m ON m.source_id = sd.source_key
WHERE sd.ncs_lclas_cd = '02'
  AND sd.sqf_field_name = '경영관리'
  AND sd.job_name = '경영지원'
GROUP BY sd.source_key
ORDER BY CAST(NULLIF(sd.duty_level, '') AS INTEGER), sd.duty_name;

-- 4. 특정 NCS 능력단위가 연결될 수 있는 SQF 직무수준
-- :unit_code 값을 실제 능력단위 코드로 바꿔 실행한다.
SELECT
  m.relation,
  m.score,
  m.confidence,
  m.review_status,
  sd.source_key,
  sd.sqf_field_name,
  sd.job_name,
  sd.duty_name,
  sd.duty_level,
  m.evidence_text
FROM sqf_ncs_matches m
JOIN sqf_duties sd ON sd.source_key = m.source_id
WHERE m.target_id = :unit_code
  AND m.review_status != 'rejected'
ORDER BY m.score DESC, sd.job_name, sd.duty_level;

-- 5. 갭분석용 후보 요구 능력단위 목록
-- :sqf_source_key 값을 목표 SQF 직무수준 source_key로 바꿔 실행한다.
SELECT
  cu.unit_code,
  cu.unit_name_raw AS unit_name,
  cu.unit_level_raw AS unit_level,
  c.middle_name,
  c.small_name,
  c.sub_name,
  m.relation,
  m.score,
  m.review_status,
  m.evidence_text
FROM sqf_ncs_matches m
JOIN competency_units cu ON cu.unit_code = m.target_id
JOIN classifications c ON c.classification_id = cu.classification_id
WHERE m.source_id = :sqf_source_key
  AND m.target_type = 'ncs_competency_unit'
  AND m.review_status != 'rejected'
ORDER BY
  CASE WHEN m.review_status IN ('human_reviewed', 'reviewed') THEN 0 ELSE 1 END,
  m.score DESC,
  cu.unit_code;
"""


def database_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        table_name = row["name"]
        counts[table_name] = int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
    return counts


def schema_inventory(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    tables = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    inventory: list[dict[str, Any]] = []
    for table in tables:
        table_name = table["name"]
        columns = [
            {
                "name": row["name"],
                "type": row["type"],
                "notnull": bool(row["notnull"]),
                "default": row["dflt_value"],
                "primary_key": bool(row["pk"]),
            }
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        ]
        foreign_keys = [
            {
                "from": row["from"],
                "to_table": row["table"],
                "to": row["to"],
            }
            for row in conn.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
        ]
        indexes = [
            row["name"]
            for row in conn.execute(f"PRAGMA index_list({table_name})").fetchall()
            if not row["name"].startswith("sqlite_autoindex")
        ]
        inventory.append(
            {
                "table": table_name,
                "note": TABLE_NOTES.get(table_name, ""),
                "columns": columns,
                "foreign_keys": foreign_keys,
                "indexes": indexes,
            }
        )
    return inventory


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_sql_files(output_dir: Path) -> dict[str, str]:
    sql_dir = output_dir / "sql"
    write_text(sql_dir / "schema.sql", SCHEMA_SQL.strip() + "\n")
    write_text(sql_dir / "indexes.sql", INDEX_SQL.strip() + "\n")
    write_text(sql_dir / "sample_queries.sql", SAMPLE_QUERIES_SQL.strip() + "\n")
    return {
        "schema": str(sql_dir / "schema.sql"),
        "indexes": str(sql_dir / "indexes.sql"),
        "sample_queries": str(sql_dir / "sample_queries.sql"),
    }


def render_schema_doc(inventory: list[dict[str, Any]], counts: dict[str, int]) -> str:
    lines = [
        "# NCS-SQF SQLite Schema",
        "",
        "이 문서는 현재 SQLite 스키마를 온톨로지/MCP 조회 관점에서 요약한다.",
        "원천 텍스트는 삭제하지 않고, API 보강값과 후보 매핑은 별도 필드/테이블로 둔다.",
        "",
        "## Table Overview",
        "",
        "| Table | Rows | Purpose |",
        "| --- | ---: | --- |",
    ]
    for table in inventory:
        table_name = table["table"]
        lines.append(f"| `{table_name}` | {counts.get(table_name, 0):,} | {table['note']} |")
    lines.extend(
        [
            "",
            "## Ontology-Oriented Flow",
            "",
            "```text",
            "classifications -> competency_units -> competency_elements",
            "competency_elements -> performance_criteria",
            "competency_elements -> ksa_items",
            "sqf_duties -> sqf_ncs_matches -> competency_units",
            "```",
            "",
            "`sqf_ncs_matches`는 공식 인정 판정이 아니라 후보 매핑 객체다. "
            "`review_status`, `confidence`, `score`, `evidence_text`를 함께 확인해야 한다.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_data_dictionary(inventory: list[dict[str, Any]]) -> str:
    lines = [
        "# NCS-SQF Data Dictionary",
        "",
        "핸드오프 SQLite의 테이블/필드 설명이다. 원문 필드는 보존하고, 정제/보강/매핑 필드는 별도 컬럼에 둔다.",
    ]
    for table in inventory:
        lines.extend(
            [
                "",
                f"## `{table['table']}`",
                "",
                table["note"] or "No description.",
                "",
                "| Column | Type | Required | PK | Default |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for column in table["columns"]:
            required = "yes" if column["notnull"] else ""
            primary_key = "yes" if column["primary_key"] else ""
            default = column["default"] if column["default"] is not None else ""
            lines.append(
                f"| `{column['name']}` | `{column['type']}` | {required} | {primary_key} | {default} |"
            )
        if table["foreign_keys"]:
            lines.extend(["", "Foreign keys:"])
            for key in table["foreign_keys"]:
                lines.append(f"- `{key['from']}` -> `{key['to_table']}.{key['to']}`")
        if table["indexes"]:
            lines.extend(["", "Indexes: " + ", ".join(f"`{name}`" for name in table["indexes"])])
    return "\n".join(lines) + "\n"


def write_docs(output_dir: Path, inventory: list[dict[str, Any]], counts: dict[str, int]) -> dict[str, str]:
    docs_dir = output_dir / "docs"
    write_text(docs_dir / "schema.md", render_schema_doc(inventory, counts))
    write_text(docs_dir / "data_dictionary.md", render_data_dictionary(inventory))
    return {
        "schema_doc": str(docs_dir / "schema.md"),
        "data_dictionary": str(docs_dir / "data_dictionary.md"),
    }


def write_db_readme(
    output_dir: Path,
    source_db_path: Path,
    db_mode: str,
    target_db_path: Path | None = None,
) -> None:
    db_dir = output_dir / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    if db_mode == "none":
        body = f"""# DB File

이 패키지는 큰 SQLite 파일을 복사하지 않았다.

현재 원본 DB:

```text
{source_db_path}
```

전달용 DB 파일을 만들려면 다음 명령을 실행한다.

```powershell
python scripts\\ncs_harness.py export-package --db-mode hardlink
```

디스크 여유가 충분하고 독립 복사본이 필요하면:

```powershell
python scripts\\ncs_harness.py export-package --db-mode copy
```
"""
    else:
        body = f"""# DB File

전달용 SQLite DB가 포함되어 있다.

```text
{target_db_path}
```

원본 DB:

```text
{source_db_path}
```

DB 전달 모드: `{db_mode}`

`hardlink` 모드는 원본 DB와 같은 파일 데이터를 공유한다. 독립 복사본이 필요하면 디스크 여유를 확보한 뒤 `--db-mode copy`로 다시 생성한다.
"""
    write_text(db_dir / "README.md", body)


def materialize_db(source_db_path: Path, output_dir: Path, db_mode: str) -> dict[str, Any]:
    db_dir = output_dir / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    target = db_dir / HANDOFF_DB_NAME
    if db_mode == "none":
        write_db_readme(output_dir, source_db_path, db_mode)
        return {"mode": "none", "path": None, "source_db_path": str(source_db_path)}
    if target.exists():
        target.unlink()
    if db_mode == "copy":
        shutil.copy2(source_db_path, target)
    elif db_mode == "hardlink":
        os.link(source_db_path, target)
    else:
        raise ValueError(f"unsupported db_mode: {db_mode}")
    write_db_readme(output_dir, source_db_path, db_mode, target)
    return {
        "mode": db_mode,
        "path": str(target),
        "source_db_path": str(source_db_path),
        "bytes": target.stat().st_size,
    }


def write_package_readme(output_dir: Path, db_info: dict[str, Any]) -> str:
    db_path = db_info.get("path") or db_info.get("source_db_path")
    body = f"""# NCS-SQF Handoff Package

이 패키지는 NCS Excel/API와 SQF API를 SQLite 기반 지식베이스로 조회하기 위한 산출물이다.

## Contents

- `sql/schema.sql`: SQLite 스키마
- `sql/indexes.sql`: 조회용 인덱스
- `sql/sample_queries.sql`: NCS/SQF/매핑 확인 쿼리
- `docs/schema.md`: 스키마 개요
- `docs/data_dictionary.md`: 테이블/필드 사전
- `manifest.json`: 생성 시점, 행 수, DB 전달 모드

## DB

```text
{db_path}
```

DB 전달 모드: `{db_info['mode']}`

`hardlink` 모드는 같은 볼륨에서 공간을 거의 쓰지 않지만 원본 DB와 같은 파일 데이터를 공유한다.
독립 파일이 필요하면 디스크 여유를 확보한 뒤 `--db-mode copy`로 다시 생성한다.

## Recommended Checks

```powershell
sqlite3 data\\db\\ncs_sqf.sqlite ".tables"
sqlite3 data\\db\\ncs_sqf.sqlite ".read sql\\sample_queries.sql"
```

샘플 쿼리는 `:unit_code`, `:sqf_source_key` 같은 파라미터를 실제 값으로 바꿔 실행한다.

## Security

API 키는 포함하지 않는다. `.env`는 전달하거나 커밋하지 않는다.
"""
    write_text(output_dir / "README.md", body)
    return str(output_dir / "README.md")


def write_manifest(
    output_dir: Path,
    *,
    source_db_path: Path,
    db_info: dict[str, Any],
    counts: dict[str, int],
    inventory: list[dict[str, Any]],
) -> str:
    manifest = {
        "generated_at": now_utc(),
        "source_db_path": str(source_db_path),
        "db": db_info,
        "counts": counts,
        "tables": [item["table"] for item in inventory],
    }
    path = output_dir / "manifest.json"
    write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return str(path)


def make_zip(output_dir: Path) -> Path:
    zip_path = output_dir.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in output_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(output_dir.parent))
    return zip_path


def export_handoff_package(
    db_path: Path | str,
    output_dir: Path | str,
    *,
    db_mode: str = "none",
    zip_output: bool = False,
) -> dict[str, Any]:
    source_db_path = Path(db_path)
    if not source_db_path.exists():
        raise FileNotFoundError(f"SQLite DB does not exist: {source_db_path}")
    package_dir = Path(output_dir)
    package_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(source_db_path)
    initialize_database(conn)
    try:
        counts = database_counts(conn)
        inventory = schema_inventory(conn)
    finally:
        conn.close()

    sql_files = write_sql_files(package_dir)
    doc_files = write_docs(package_dir, inventory, counts)
    db_info = materialize_db(source_db_path, package_dir, db_mode)
    readme = write_package_readme(package_dir, db_info)
    manifest = write_manifest(
        package_dir,
        source_db_path=source_db_path,
        db_info=db_info,
        counts=counts,
        inventory=inventory,
    )
    zip_path = str(make_zip(package_dir)) if zip_output else None
    return {
        "output_dir": str(package_dir),
        "db": db_info,
        "sql": sql_files,
        "docs": doc_files,
        "readme": readme,
        "manifest": manifest,
        "zip": zip_path,
    }
