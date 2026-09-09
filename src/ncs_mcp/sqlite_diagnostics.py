"""Distinguish optional SQLite diagnostic virtual tables from application data."""
from __future__ import annotations

import re


def is_dbstat_table(create_sql: str | None) -> bool:
    # dbstat exposes file-page statistics, has no persisted application rows,
    # and may be absent in a different SQLite build opening the same database.
    sql = create_sql or ""
    return bool(re.match(r"\s*CREATE\s+VIRTUAL\s+TABLE\b", sql, re.I)
                and re.search(r'\bUSING\s+["`\[]?dbstat["`\]]?(?=\s|\(|$)', sql, re.I))
