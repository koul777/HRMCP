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
    sqf_service_key: str | None
    study_module_service_key: str | None
    training_course_service_key: str | None
    qualification_service_key: str | None
    job_base_service_key: str | None
    reports_dir: Path
    operator_tools_enabled: bool
    advanced_tools_enabled: bool
    read_only_mode: bool
    max_concurrent_recommendations: int
    recommendation_queue_timeout_seconds: float


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


def read_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value:
            values[key] = value
    return values


def load_settings() -> Settings:
    env_path = PROJECT_ROOT / ".env"
    if load_dotenv:
        load_dotenv(env_path)
    load_env_file(env_path)
    file_values = read_env_values(env_path)

    def value_for(*keys: str) -> str | None:
        for key in keys:
            value = os.getenv(key) or file_values.get(key)
            if value:
                return value
        return None

    def bool_for(*keys: str) -> bool:
        value = value_for(*keys)
        if value is None:
            return False
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}

    def int_for(key: str, default: int, *, minimum: int, maximum: int) -> int:
        value = value_for(key)
        try:
            parsed = int(value) if value is not None else default
        except ValueError:
            parsed = default
        return max(minimum, min(maximum, parsed))

    def float_for(key: str, default: float, *, minimum: float, maximum: float) -> float:
        value = value_for(key)
        try:
            parsed = float(value) if value is not None else default
        except ValueError:
            parsed = default
        return max(minimum, min(maximum, parsed))

    excel_value = value_for("NCS_EXCEL_PATH")
    db_value = value_for("NCS_DB_PATH")
    service_key = value_for("NCS_SERVICE_KEY")
    sqf_service_key = value_for("NCS_SQF_SERVICE_KEY")
    study_module_service_key = value_for(
        "NCS_STUDY_MODULE_SERVICE_KEY",
        "NCS_LEARNING_MODULE_SERVICE_KEY",
        "NCS_SERVICE_KEY",
    )
    training_course_service_key = value_for(
        "NCS_TRAINING_COURSE_SERVICE_KEY",
        "NCS_SERVICE_KEY",
    )
    qualification_service_key = value_for(
        "NCS_QUALIFICATION_SERVICE_KEY",
        "NCS_NCS_CL_CD_JM_SERVICE_KEY",
        "NCS_SERVICE_KEY",
    )
    job_base_service_key = value_for(
        "NCS_JOB_BASE_SERVICE_KEY",
        "NCS_JOB_BASE_COMPETENCY_SERVICE_KEY",
        "NCS_SERVICE_KEY",
    )
    reports_value = value_for("NCS_REPORTS_DIR")
    operator_tools_enabled = bool_for("NCS_MCP_ENABLE_OPERATOR_TOOLS")
    # Advanced ontology / education-integration / transition tools are hidden by
    # default for the public release. Set NCS_MCP_ENABLE_ADVANCED_TOOLS=1 to expose
    # them again once they are stabilized.
    advanced_tools_enabled = bool_for("NCS_MCP_ENABLE_ADVANCED_TOOLS")
    read_only_mode = bool_for("NCS_MCP_READ_ONLY")
    max_concurrent_recommendations = int_for(
        "NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS", 2, minimum=1, maximum=32
    )
    recommendation_queue_timeout_seconds = float_for(
        "NCS_MCP_RECOMMENDATION_QUEUE_TIMEOUT_SECONDS",
        30.0,
        minimum=0.1,
        maximum=300.0,
    )

    return Settings(
        excel_path=Path(excel_value) if excel_value else None,
        db_path=Path(db_value) if db_value else DEFAULT_DB_PATH,
        service_key=service_key,
        sqf_service_key=sqf_service_key,
        study_module_service_key=study_module_service_key,
        training_course_service_key=training_course_service_key,
        qualification_service_key=qualification_service_key,
        job_base_service_key=job_base_service_key,
        reports_dir=Path(reports_value) if reports_value else DEFAULT_REPORTS_DIR,
        operator_tools_enabled=operator_tools_enabled,
        advanced_tools_enabled=advanced_tools_enabled,
        read_only_mode=read_only_mode,
        max_concurrent_recommendations=max_concurrent_recommendations,
        recommendation_queue_timeout_seconds=recommendation_queue_timeout_seconds,
    )
