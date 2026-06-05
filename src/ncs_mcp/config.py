from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency guard
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "processed" / "ncs.db"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"


@dataclass(frozen=True)
class Settings:
    excel_path: Path | None
    db_path: Path
    service_key: str | None
    reports_dir: Path


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not os.environ.get(key):
            os.environ[key] = value


def load_settings() -> Settings:
    if load_dotenv:
        load_dotenv(PROJECT_ROOT / ".env")
    load_env_file(PROJECT_ROOT / ".env")

    excel_value = os.getenv("NCS_EXCEL_PATH")
    db_value = os.getenv("NCS_DB_PATH")
    service_key = os.getenv("NCS_SERVICE_KEY") or None
    reports_value = os.getenv("NCS_REPORTS_DIR")

    return Settings(
        excel_path=Path(excel_value) if excel_value else None,
        db_path=Path(db_value) if db_value else DEFAULT_DB_PATH,
        service_key=service_key,
        reports_dir=Path(reports_value) if reports_value else DEFAULT_REPORTS_DIR,
    )
