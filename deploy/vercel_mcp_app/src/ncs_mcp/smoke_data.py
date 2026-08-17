from __future__ import annotations

import argparse
import json
from pathlib import Path

from ncs_mcp.db import connect, initialize_database, now_utc


def create_ready_smoke_db(db_path: Path) -> dict[str, object]:
    """Create the smallest SQLite DB that satisfies MCP readiness checks."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        initialize_database(conn)
        timestamp = now_utc()
        conn.execute(
            """
            INSERT OR IGNORE INTO classifications(
                major_code, major_name, middle_code, middle_name,
                small_code, small_name, sub_code, sub_name
            ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
            """
        )
        classification_id = conn.execute(
            """
            SELECT classification_id
            FROM classifications
            WHERE major_code = '02'
              AND middle_code = '02'
              AND small_code = '02'
              AND sub_code = '01'
            """
        ).fetchone()["classification_id"]
        conn.execute(
            """
            INSERT OR IGNORE INTO competency_units(
                unit_code, base_unit_code, unit_version, unit_name_raw,
                unit_level_raw, classification_id, api_match_status,
                created_at, updated_at
            ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                      '5', ?, 'matched', ?, ?)
            """,
            (classification_id, timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO competency_elements(
                unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
            ) VALUES ('0202020101_23v3', '1', '0202020101_23v3 1', 'Plan workforce', '5')
            """
        )
        element_id = conn.execute(
            """
            SELECT element_id
            FROM competency_elements
            WHERE unit_code = '0202020101_23v3'
              AND element_no = '1'
            """
        ).fetchone()["element_id"]
        conn.execute(
            """
            INSERT OR IGNORE INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
            VALUES (?, '1', 'Build a workforce plan from business strategy.')
            """,
            (element_id,),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
            VALUES (?, '01', 'knowledge', '1', 'workforce planning')
            """,
            (element_id,),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO ncs_training_courses(
                ncs_cl_cd, compe_unit_name, train_goal, train_time, api_fetched_at
            ) VALUES ('0202020101_23v3', 'HR planning', 'Build HR planning capability.', '24', ?)
            """,
            (timestamp,),
        )
        conn.commit()
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "competency_units",
                "performance_criteria",
                "ksa_items",
                "ncs_training_courses",
            )
        }
    finally:
        conn.close()
    return {"ok": all(count > 0 for count in counts.values()), "db": str(db_path), "counts": counts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a minimal ready DB for MCP smoke checks.")
    parser.add_argument("--out", required=True, help="SQLite DB path to create or update.")
    args = parser.parse_args(argv)
    result = create_ready_smoke_db(Path(args.out))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
