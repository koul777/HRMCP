from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ncs_mcp.db import clamp_limit, normalize_spaces, rows_to_dicts


SCHEMA_VERSION = "ncs_ontology_adjusted_education_systems_v1"
FIELD_REVIEW_SCHEMA_VERSION = "ncs_ontology_transferability_field_review_v2"
REVIEW_SEED_SCHEMA_VERSION = "ncs_ontology_transferability_review_seed_v1"
REVIEW_SEEDPACK_SCHEMA_VERSION = "ncs_ontology_transferability_review_seedpack_v1"
CALIBRATION_SCHEMA_VERSION = "ncs_ontology_transferability_calibration_v1"
MAJOR_RUN_SCHEMA_VERSION = "ncs_ontology_transferability_major_run_v1"
SPOTCHECK_PLAN_SCHEMA_VERSION = "ncs_ontology_transferability_spotcheck_plan_v1"
METHOD_WORK_QUEUE_SCHEMA_VERSION = "ncs_ontology_transferability_method_work_queue_v1"
ARTIFACT_AUDIT_SCHEMA_VERSION = "ncs_ontology_transferability_artifact_audit_v1"
RELEASE_GATE_SCHEMA_VERSION = "ncs_ontology_transferability_release_gate_v1"
EDUCATION_SYSTEM_AUDIT_SCHEMA_VERSION = "ncs_ontology_transferability_education_system_audit_v1"
COURSE_LINK_GAP_DIAGNOSTIC_SCHEMA_VERSION = "ncs_training_course_link_gap_diagnostic_v1"
COURSE_LINK_CANDIDATE_REVIEW_SCHEMA_VERSION = "ncs_training_course_link_candidate_review_v1"
ALLOWED_REVIEW_DECISIONS = ["approve", "reject", "defer"]
COURSE_SCOPE_LINK_CANDIDATE_STATUSES = {
    "same_sub_classification",
    "same_small_classification",
    "same_middle_classification",
    "same_major_only",
}


def _clean(value: Any) -> str:
    return normalize_spaces("" if value is None else str(value))


def _csv_cell(value: Any) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = str(value).lower()
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text[:1] in {"=", "+", "-", "@"} or text.lstrip()[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _unit_label(unit: dict[str, Any]) -> str:
    return _clean(unit.get("unit_name_raw")) or _clean(unit.get("unit_code"))


def _scope_label(scope: dict[str, Any]) -> str:
    parts = [
        scope.get("major_name"),
        scope.get("middle_name"),
        scope.get("small_name"),
        scope.get("sub_name"),
    ]
    return " > ".join(_clean(part) for part in parts if _clean(part))


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash_payload(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file_fingerprint(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=False)
    fingerprint: dict[str, Any] = {
        "path": str(path),
        "resolved_path": str(resolved),
        "exists": path.exists(),
        "size_bytes": None,
        "sha256": "",
    }
    if not path.exists() or not path.is_file():
        return fingerprint
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    fingerprint["size_bytes"] = path.stat().st_size
    fingerprint["sha256"] = digest.hexdigest()
    return fingerprint


def _portable_path(path: Path) -> str:
    if not path.is_absolute():
        return str(path)
    try:
        return str(path.resolve(strict=False).relative_to(Path.cwd().resolve(strict=False)))
    except ValueError:
        return path.name


def _public_file_fingerprint(path: Path) -> dict[str, Any]:
    fingerprint = dict(_file_fingerprint(path))
    fingerprint["path"] = _portable_path(path)
    fingerprint.pop("resolved_path", None)
    return fingerprint


def _seedpack_id(exported_at: str, source_run: Path, seed_count: int) -> str:
    compact = exported_at.replace(":", "").replace("-", "")
    compact = compact.replace("+0000", "Z").replace("+00:00", "Z")
    suffix = _hash_payload({"source_run": str(source_run), "seed_count": seed_count})[:12]
    return f"ontology-transferability-review-{compact}-{suffix}"


def _resolve_artifact_path(value: Any, *, base_path: Path) -> Path:
    path = Path(_clean(value))
    if path.is_absolute() or path.exists():
        return path
    base_candidate = base_path.parent / path
    if base_candidate.exists():
        return base_candidate
    return path


def _resolved_reference(value: Any, *, base_path: Path) -> Path:
    return _resolve_artifact_path(value, base_path=base_path).resolve(strict=False)


def _same_reference(left: Any, right: Any, *, left_base: Path, right_base: Path) -> bool:
    if not _clean(left) or not _clean(right):
        return False
    return _resolved_reference(left, base_path=left_base) == _resolved_reference(right, base_path=right_base)


def _target_level_band(level: Any) -> dict[str, Any]:
    text = _clean(level)
    try:
        numeric = int(float(text))
    except ValueError:
        return {"code": "unknown", "label": "Unknown", "level": text}
    if numeric <= 3:
        code = "level_1_3"
        label = "L1-L3"
    elif numeric <= 4:
        code = "level_4"
        label = "L4"
    elif numeric <= 6:
        code = "level_5_6"
        label = "L5-L6"
    else:
        code = "level_7_plus"
        label = "L7+"
    return {"code": code, "label": label, "level": text}


def _qualification_key(row: dict[str, Any]) -> str:
    return ":".join(
        _clean(row.get(key))
        for key in ("jm_cd", "organ_std_ver_cd", "ablt_unit_typ_cd", "min_edu_trng_tm")
        if _clean(row.get(key))
    )


def _job_base_key(row: dict[str, Any]) -> str:
    return f"{row.get('job_base_competency_id')}:{row.get('job_base_factor_id')}"


def _pair_key(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left <= right else (right, left)


def _fetch_scopes(
    conn: sqlite3.Connection,
    *,
    major_code: str | None,
    limit_scopes: int | None,
    min_units: int,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if major_code:
        where = "WHERE c.major_code = ?"
        params.append(major_code)
    query_params = [*params, min_units]
    limit_clause = ""
    if limit_scopes is not None:
        limit_clause = "LIMIT ?"
        query_params.append(clamp_limit(limit_scopes, default=100, maximum=100000))
    return rows_to_dicts(
        conn.execute(
            f"""
            SELECT
                c.classification_id, c.major_code, c.major_name,
                c.middle_code, c.middle_name, c.small_code, c.small_name,
                c.sub_code, c.sub_name,
                COUNT(DISTINCT cu.unit_code) AS unit_count
            FROM classifications c
            JOIN competency_units cu ON cu.classification_id = c.classification_id
            {where}
            GROUP BY
                c.classification_id, c.major_code, c.major_name,
                c.middle_code, c.middle_name, c.small_code, c.small_name,
                c.sub_code, c.sub_name
            HAVING COUNT(DISTINCT cu.unit_code) >= ?
            ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code
            {limit_clause}
            """,
            tuple(query_params),
        ).fetchall()
    )


def _fetch_units_by_scope(conn: sqlite3.Connection, scope_ids: set[int]) -> dict[int, list[dict[str, Any]]]:
    if not scope_ids:
        return {}
    placeholders = ",".join("?" for _ in scope_ids)
    rows = rows_to_dicts(
        conn.execute(
            f"""
            SELECT
                cu.unit_code, cu.unit_name_raw, cu.unit_level_raw, cu.classification_id,
                c.major_code, c.major_name, c.middle_code, c.middle_name,
                c.small_code, c.small_name, c.sub_code, c.sub_name
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE cu.classification_id IN ({placeholders})
            ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code, cu.unit_code
            """,
            tuple(sorted(scope_ids)),
        ).fetchall()
    )
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["classification_id"])].append(row)
    return grouped


def _load_unit_concepts(conn: sqlite3.Connection, unit_codes: set[str]) -> dict[str, set[int]]:
    concepts: dict[str, set[int]] = {code: set() for code in unit_codes}
    if not unit_codes:
        return concepts
    payload = json.dumps(sorted(unit_codes))
    rows = conn.execute(
        """
        SELECT ce.unit_code, kcl.concept_id
        FROM competency_elements ce
        JOIN ksa_items ki ON ki.element_id = ce.element_id
        JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
        WHERE ce.unit_code IN (SELECT value FROM json_each(?))
        """,
        (payload,),
    ).fetchall()
    for unit_code, concept_id in rows:
        if concept_id is not None:
            concepts[_clean(unit_code)].add(int(concept_id))
    rows = conn.execute(
        """
        SELECT ce.unit_code, kacl.concept_id
        FROM competency_elements ce
        JOIN ksa_atomic_items kai ON kai.element_id = ce.element_id
        JOIN ksa_atomic_concept_links kacl ON kacl.atomic_id = kai.atomic_id
        WHERE ce.unit_code IN (SELECT value FROM json_each(?))
        """,
        (payload,),
    ).fetchall()
    for unit_code, concept_id in rows:
        if concept_id is not None:
            concepts[_clean(unit_code)].add(int(concept_id))
    return concepts


def _load_relation_adjacency(conn: sqlite3.Connection, concept_ids: set[int]) -> dict[int, set[int]]:
    adjacency: dict[int, set[int]] = defaultdict(set)
    if not concept_ids:
        return adjacency
    concept_list = sorted(concept_ids)
    chunk_size = 900
    for offset in range(0, len(concept_list), chunk_size):
        chunk = concept_list[offset : offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT source_concept_id, target_concept_id
            FROM ontology_concept_relations
            WHERE review_status != 'rejected'
              AND (
                    source_concept_id IN ({placeholders})
                    OR target_concept_id IN ({placeholders})
              )
            """,
            (*chunk, *chunk),
        ).fetchall()
        for source_id, target_id in rows:
            source = int(source_id)
            target = int(target_id)
            if source in concept_ids and target in concept_ids:
                adjacency[source].add(target)
                adjacency[target].add(source)
    return adjacency


def _load_task_similarity_for_scope(
    conn: sqlite3.Connection,
    unit_codes: set[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not unit_codes:
        return {}
    placeholders = ",".join("?" for _ in unit_codes)
    rows = rows_to_dicts(
        conn.execute(
            f"""
            SELECT
                source_unit_code, target_unit_code,
                similarity_score, shared_concept_count
            FROM task_similarity_links
            WHERE review_status != 'rejected'
              AND source_unit_code IN ({placeholders})
              AND target_unit_code IN ({placeholders})
            """,
            (*sorted(unit_codes), *sorted(unit_codes)),
        ).fetchall()
    )
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        left = _clean(row.get("source_unit_code"))
        right = _clean(row.get("target_unit_code"))
        if not left or not right or left == right:
            continue
        key = _pair_key(left, right)
        score = float(row.get("similarity_score") or 0.0)
        item = by_pair.setdefault(
            key,
            {"max_score": 0.0, "link_count": 0, "shared_concept_count_max": 0},
        )
        item["link_count"] += 1
        if score > item["max_score"]:
            item["max_score"] = score
        shared = int(row.get("shared_concept_count") or 0)
        if shared > item["shared_concept_count_max"]:
            item["shared_concept_count_max"] = shared
    return by_pair


def _load_job_base_keys(conn: sqlite3.Connection, unit_codes: set[str]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {code: set() for code in unit_codes}
    if not unit_codes:
        return result
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT unit_code, job_base_competency_id, job_base_factor_id
            FROM ncs_unit_job_base_links
            WHERE unit_code IN (SELECT value FROM json_each(?))
            """,
            (json.dumps(sorted(unit_codes)),),
        ).fetchall()
    )
    for row in rows:
        result[_clean(row.get("unit_code"))].add(_job_base_key(row))
    return result


def _load_qualification_keys(conn: sqlite3.Connection, unit_codes: set[str]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {code: set() for code in unit_codes}
    if not unit_codes:
        return result
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT unit_code, jm_cd, organ_std_ver_cd, ablt_unit_typ_cd, min_edu_trng_tm
            FROM ncs_unit_qualification_links
            WHERE unit_code IN (SELECT value FROM json_each(?))
            """,
            (json.dumps(sorted(unit_codes)),),
        ).fetchall()
    )
    for row in rows:
        key = _qualification_key(row)
        if key:
            result[_clean(row.get("unit_code"))].add(key)
    return result


def _load_course_links(conn: sqlite3.Connection, unit_codes: set[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {
        code: {
            "linked_training_course_count": 0,
            "sample_course_names": [],
            "methods": [],
            "facilities": [],
            "hours": [],
        }
        for code in unit_codes
    }
    if not unit_codes:
        return result
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT
                l.unit_code, tc.training_course_id, tc.compe_unit_name,
                tc.train_time, tc.meth_name, tc.fac_name
            FROM ncs_training_course_unit_links l
            JOIN ncs_training_courses tc ON tc.training_course_id = l.training_course_id
            WHERE l.unit_code IN (SELECT value FROM json_each(?))
              AND l.review_status != 'rejected'
            ORDER BY l.unit_code, tc.training_course_id
            """,
            (json.dumps(sorted(unit_codes)),),
        ).fetchall()
    )
    seen_courses: dict[str, set[int]] = defaultdict(set)
    for row in rows:
        unit_code = _clean(row.get("unit_code"))
        course_id = int(row.get("training_course_id") or 0)
        if course_id in seen_courses[unit_code]:
            continue
        seen_courses[unit_code].add(course_id)
        item = result[unit_code]
        item["linked_training_course_count"] += 1
        course_name = _clean(row.get("compe_unit_name"))
        if course_name and course_name not in item["sample_course_names"] and len(item["sample_course_names"]) < 3:
            item["sample_course_names"].append(course_name)
        method = _clean(row.get("meth_name"))
        if method and method not in item["methods"] and len(item["methods"]) < 5:
            item["methods"].append(method)
        facility = _clean(row.get("fac_name"))
        if facility and facility not in item["facilities"] and len(item["facilities"]) < 5:
            item["facilities"].append(facility)
        hours = _clean(row.get("train_time"))
        if hours and hours not in item["hours"] and len(item["hours"]) < 5:
            item["hours"].append(hours)
    return result


def _related_target_count(
    source_ids: set[int],
    target_ids: set[int],
    exact_ids: set[int],
    adjacency: dict[int, set[int]],
) -> int:
    if not source_ids or not target_ids:
        return 0
    related: set[int] = set()
    for concept_id in source_ids:
        related.update(adjacency.get(concept_id, set()))
    return len((related & target_ids) - exact_ids)


def _semantic_fit(
    *,
    source_unit: str,
    target_unit: str,
    source_ids: set[int],
    target_ids: set[int],
    source_job_base: set[str],
    target_job_base: set[str],
    source_qualifications: set[str],
    target_qualifications: set[str],
    adjacency: dict[int, set[int]],
    task_similarity: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    exact_ids = source_ids & target_ids
    exact_ratio = round(len(exact_ids) / len(target_ids), 4) if target_ids else 0.0
    related_count = _related_target_count(source_ids, target_ids, exact_ids, adjacency)
    related_ratio = related_count / len(target_ids) if target_ids else 0.0
    task = task_similarity.get(_pair_key(source_unit, target_unit), {})
    task_max = float(task.get("max_score") or 0.0)
    job_base_ratio = len(source_job_base & target_job_base) / len(target_job_base) if target_job_base else 0.0
    qualification_ratio = (
        len(source_qualifications & target_qualifications) / len(target_qualifications)
        if target_qualifications
        else 0.0
    )
    components = {
        "exact_ksa_overlap_ratio": exact_ratio,
        "scope_containment_score": 0.0,
        "classification_scope_score": 0.24,
        "ontology_related_score": round(min(0.14, 0.35 * related_ratio), 4),
        "task_similarity_score": round(min(0.1, 0.2 * task_max), 4),
        "job_base_score": round(min(0.05, 0.12 * job_base_ratio), 4),
        "qualification_score": round(min(0.03, 0.08 * qualification_ratio), 4),
        "role_overlay_score": 0.0,
    }
    component_total = sum(float(value) for value in components.values())
    adjusted = round(min(1.0, component_total), 4)
    ratio = max(exact_ratio, adjusted)
    adjusted_minus_exact = round(max(0.0, ratio - exact_ratio), 4)
    baseline_dependency_ratio = round(adjusted_minus_exact / ratio, 4) if ratio else 0.0
    return {
        "source_unit_code": source_unit,
        "target_unit_code": target_unit,
        "exact_ksa_overlap_ratio": exact_ratio,
        "ontology_adjusted_transferability_ratio": ratio,
        "adjusted_minus_exact": adjusted_minus_exact,
        "baseline_dependency_ratio": baseline_dependency_ratio,
        "components": components,
        "component_share": {
            key: round(float(value) / component_total, 4) if component_total else 0.0
            for key, value in components.items()
        },
        "scope_relation": "same_sub_classification",
        "ontology_related_ksa_count": related_count,
        "task_similarity_max_score": round(task_max, 4),
        "task_similarity_link_count": int(task.get("link_count") or 0),
        "shared_exact_ksa_count": len(exact_ids),
        "source_ksa_concept_count": len(source_ids),
        "target_ksa_concept_count": len(target_ids),
        "transfer_gap_ksa_count": max(0, len(target_ids - source_ids)),
    }


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _score_value(score: dict[str, Any], key: str) -> float:
    return float(score.get(key) or 0.0)


def _is_baseline_heavy_scope(score: dict[str, Any]) -> bool:
    return _score_value(score, "baseline_heavy_pair_ratio") >= 0.2 or (
        _score_value(score, "avg_adjusted") >= 0.34
        and _score_value(score, "avg_exact") <= 0.02
    )


def _scope_plausibility(score: dict[str, Any]) -> str:
    if (
        _score_value(score, "avg_adjusted") >= 0.5
        and _score_value(score, "avg_exact") >= 0.15
        and _score_value(score, "baseline_heavy_pair_ratio") <= 0.1
    ):
        return "strong_exact_and_adjusted_support"
    if _is_baseline_heavy_scope(score):
        return "needs_review_baseline_or_relation_heavy"
    return "plausible_draft_needs_sampling"


def _scope_top_hubs(scope: dict[str, Any], *, limit: int = 5) -> list[str]:
    groups = (scope.get("education_system") or {}).get("groups") or {}
    hubs = groups.get("common_transfer_hub") or []
    if not hubs:
        hubs = scope.get("average_by_unit") or []
    return [_clean(item.get("unit_name")) for item in hubs[:limit] if _clean(item.get("unit_name"))]


def _matrix_sample(scope: dict[str, Any], *, limit: int = 8) -> list[dict[str, Any]]:
    rows = (scope.get("education_system") or {}).get("training_system_matrix") or []
    sample: list[dict[str, Any]] = []
    for row in rows[:limit]:
        task_basis = row.get("task_ksa_basis") or {}
        course_link = row.get("course_link") or {}
        education_type = row.get("education_type") or {}
        required = row.get("required_optional_basis") or {}
        human_review = row.get("human_review") or {}
        sample.append(
            {
                "unit_name": row.get("unit_name"),
                "unit_code": row.get("unit_code"),
                "education_type": education_type.get("code"),
                "required_optional": required.get("code"),
                "avg_adjusted": task_basis.get("average_adjusted_transferability"),
                "avg_exact": task_basis.get("average_exact_ksa_overlap"),
                "baseline_dependency_ratio": task_basis.get("baseline_dependency_ratio"),
                "course_count": course_link.get("linked_training_course_count", 0),
                "sample_courses": course_link.get("sample_course_names") or [],
                "human_review_flags": human_review.get("flags") or [],
            }
        )
    return sample


def _load_seedpack_records(seedpack_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records = [
        json.loads(line)
        for line in seedpack_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not records:
        raise ValueError("Ontology transferability seedpack is empty.")
    batch = records[0]
    if batch.get("record_type") != "batch":
        raise ValueError("Ontology transferability seedpack must start with a batch record.")
    if batch.get("schema") != REVIEW_SEEDPACK_SCHEMA_VERSION:
        raise ValueError(
            f"Invalid ontology transferability seedpack schema: {batch.get('schema')!r}. "
            f"Expected {REVIEW_SEEDPACK_SCHEMA_VERSION!r}."
        )
    items = [
        record
        for record in records[1:]
        if record.get("record_type") == "ontology_transferability_review_item"
    ]
    if len(items) != int(batch.get("seed_count") or 0):
        raise ValueError(
            f"Seedpack item count mismatch: header={batch.get('seed_count')}, records={len(items)}"
        )
    return batch, items


def _scope_review_flags(scope: dict[str, Any]) -> list[str]:
    score = scope.get("score_summary") or {}
    flags: list[str] = []
    if _score_value(score, "baseline_heavy_pair_ratio") >= 0.2:
        flags.append("baseline_heavy_pair_ratio_ge_0.2")
    if _score_value(score, "avg_adjusted") >= 0.32 and _score_value(score, "avg_exact") <= 0.02:
        flags.append("low_exact_high_adjusted")
    matrix = (scope.get("education_system") or {}).get("training_system_matrix") or []
    if matrix and not any(
        int(((row.get("course_link") or {}).get("linked_training_course_count") or 0)) > 0
        for row in matrix
    ):
        flags.append("no_direct_training_course_links_in_matrix")
    return flags


def _scope_has_any_course_link(scope: dict[str, Any]) -> bool:
    matrix = (scope.get("education_system") or {}).get("training_system_matrix") or []
    return any(
        int(((row.get("course_link") or {}).get("linked_training_course_count") or 0)) > 0
        for row in matrix
    )


def _score_band(value: float, bands: list[tuple[str, float | None, float | None]]) -> str:
    for label, minimum, maximum in bands:
        if minimum is not None and value < minimum:
            continue
        if maximum is not None and value >= maximum:
            continue
        return label
    return "unbanded"


def _load_major_transferability_reports(major_run_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = _load_json(major_run_path)
    if run.get("schema") != MAJOR_RUN_SCHEMA_VERSION:
        raise ValueError(
            f"Invalid ontology transferability run schema: {run.get('schema')!r}. "
            f"Expected {MAJOR_RUN_SCHEMA_VERSION!r}."
        )
    results = run.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("Ontology transferability run manifest must contain a non-empty results list.")
    loaded: list[dict[str, Any]] = []
    for index, result in enumerate(results, start=1):
        if not isinstance(result, dict):
            raise ValueError(f"Run result {index} must be an object.")
        missing_fields = [
            field
            for field in ("major_code", "returncode", "json_path")
            if result.get(field) in (None, "")
        ]
        if missing_fields:
            raise ValueError(f"Run result {index} is missing required fields: {', '.join(missing_fields)}")
        artifact_path = _resolve_artifact_path(result.get("json_path"), base_path=major_run_path)
        report: dict[str, Any] | None = None
        load_error = ""
        if int(result.get("returncode") or 0) == 0 and artifact_path.exists():
            try:
                report = _load_json(artifact_path)
            except (OSError, json.JSONDecodeError) as exc:
                load_error = str(exc)
        elif not artifact_path.exists():
            load_error = f"artifact_not_found: {artifact_path}"
        loaded.append(
            {
                "run_result": result,
                "artifact_path": artifact_path,
                "report": report,
                "load_error": load_error,
            }
        )
    return run, loaded


def _increment(counter: dict[str, int], key: Any) -> None:
    cleaned = _clean(key) or "unknown"
    counter[cleaned] = int(counter.get(cleaned) or 0) + 1


def _row_link_count(row: dict[str, Any]) -> int:
    try:
        return int(((row.get("course_link") or {}).get("linked_training_course_count") or 0))
    except (TypeError, ValueError):
        return 0


def _review_status_is_unsafe(status: Any) -> bool:
    return _clean(status).lower() in {
        "accepted",
        "approved",
        "human_reviewed",
        "reviewed",
    }


def build_ontology_transferability_education_system_audit(major_run_path: Path) -> dict[str, Any]:
    run, loaded_reports = _load_major_transferability_reports(major_run_path)
    source_run_fingerprint = _public_file_fingerprint(major_run_path)
    aggregate: dict[str, Any] = {
        "major_count": 0,
        "scope_count": 0,
        "matrix_row_count": 0,
        "recommended_path_stage_count": 0,
        "matrix_rows_with_course_links": 0,
        "matrix_rows_without_course_links": 0,
        "rows_requiring_human_review": 0,
        "rows_with_low_exact_flag": 0,
        "rows_with_baseline_heavy_flag": 0,
        "unsafe_review_status_count": 0,
        "invalid_review_status_count": 0,
        "required_optional_counts": {},
        "education_type_counts": {},
        "delivery_operation_counts": {},
        "facility_fit_counts": {},
        "human_review_status_counts": {},
        "guide_stage_counts": {},
    }
    major_rows: list[dict[str, Any]] = []
    priority_scopes: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    load_errors: list[dict[str, Any]] = []

    for item in loaded_reports:
        result = item["run_result"]
        report = item["report"]
        if report is None:
            load_error = {
                "major_code": result.get("major_code"),
                "major_name": result.get("major_name"),
                "returncode": result.get("returncode"),
                "json_path": result.get("json_path"),
                "load_error": item.get("load_error"),
            }
            load_errors.append(load_error)
            findings.append(
                {
                    "severity": "blocker",
                    "code": "major_artifact_load_failed",
                    "message": "A per-major ontology-transferability artifact could not be loaded.",
                    **load_error,
                }
            )
            major_rows.append(
                {
                    "major_code": result.get("major_code"),
                    "major_name": result.get("major_name"),
                    "ok": False,
                    "load_error": item.get("load_error"),
                }
            )
            continue
        if report.get("schema") != SCHEMA_VERSION:
            schema_error = {
                "major_code": result.get("major_code"),
                "major_name": result.get("major_name"),
                "returncode": result.get("returncode"),
                "json_path": result.get("json_path"),
                "schema": report.get("schema"),
                "expected_schema": SCHEMA_VERSION,
            }
            load_errors.append(schema_error)
            findings.append(
                {
                    "severity": "blocker",
                    "code": "major_artifact_schema_mismatch",
                    "message": "A per-major ontology-transferability artifact has an invalid schema.",
                    **schema_error,
                }
            )
            major_rows.append(
                {
                    "major_code": result.get("major_code"),
                    "major_name": result.get("major_name"),
                    "ok": False,
                    "schema": report.get("schema"),
                    "expected_schema": SCHEMA_VERSION,
                }
            )
            continue

        aggregate["major_count"] += 1
        major_counts: dict[str, Any] = {
            "scope_count": 0,
            "matrix_row_count": 0,
            "recommended_path_stage_count": 0,
            "matrix_rows_with_course_links": 0,
            "matrix_rows_without_course_links": 0,
            "rows_requiring_human_review": 0,
            "rows_with_low_exact_flag": 0,
            "rows_with_baseline_heavy_flag": 0,
            "unsafe_review_status_count": 0,
            "invalid_review_status_count": 0,
            "required_optional_counts": {},
            "education_type_counts": {},
            "delivery_operation_counts": {},
            "facility_fit_counts": {},
            "human_review_status_counts": {},
            "guide_stage_counts": {},
        }

        for scope in report.get("scopes") or []:
            education_system = scope.get("education_system") or {}
            matrix = education_system.get("training_system_matrix") or []
            recommended_path = education_system.get("recommended_path") or []
            score = scope.get("score_summary") or {}
            scope_flags = set(_scope_review_flags(scope))
            scope_without_course_links = 0
            scope_review_rows = 0
            scope_unsafe_rows = 0
            scope_invalid_review_rows = 0

            aggregate["scope_count"] += 1
            major_counts["scope_count"] += 1
            aggregate["recommended_path_stage_count"] += len(recommended_path)
            major_counts["recommended_path_stage_count"] += len(recommended_path)

            for stage in recommended_path:
                if isinstance(stage, dict):
                    _increment(aggregate["guide_stage_counts"], stage.get("guide_stage"))
                    _increment(major_counts["guide_stage_counts"], stage.get("guide_stage"))

            if not isinstance(matrix, list) or not matrix:
                findings.append(
                    {
                        "severity": "review",
                        "code": "missing_training_system_matrix",
                        "message": "Scope has no training_system_matrix rows.",
                        "major_code": result.get("major_code"),
                        "major_name": result.get("major_name"),
                        "scope_label": scope.get("scope_label"),
                    }
                )
                continue

            aggregate["matrix_row_count"] += len(matrix)
            major_counts["matrix_row_count"] += len(matrix)
            for row in matrix:
                if not isinstance(row, dict):
                    continue
                link_count = _row_link_count(row)
                if link_count > 0:
                    aggregate["matrix_rows_with_course_links"] += 1
                    major_counts["matrix_rows_with_course_links"] += 1
                else:
                    aggregate["matrix_rows_without_course_links"] += 1
                    major_counts["matrix_rows_without_course_links"] += 1
                    scope_without_course_links += 1

                required_code = ((row.get("required_optional_basis") or {}).get("code") or "unknown")
                education_code = ((row.get("education_type") or {}).get("code") or "unknown")
                delivery_code = ((row.get("delivery_operation") or {}).get("code") or "unknown")
                facility_status = ((row.get("facility_constraint_fit") or {}).get("status") or "unknown")
                human_review = row.get("human_review") or {}
                review_status = human_review.get("status") or "missing"
                flags = human_review.get("flags") or []

                for counter_name, value in [
                    ("required_optional_counts", required_code),
                    ("education_type_counts", education_code),
                    ("delivery_operation_counts", delivery_code),
                    ("facility_fit_counts", facility_status),
                    ("human_review_status_counts", review_status),
                ]:
                    _increment(aggregate[counter_name], value)
                    _increment(major_counts[counter_name], value)

                if review_status == "needs_review":
                    aggregate["rows_requiring_human_review"] += 1
                    major_counts["rows_requiring_human_review"] += 1
                    scope_review_rows += 1
                else:
                    aggregate["invalid_review_status_count"] += 1
                    major_counts["invalid_review_status_count"] += 1
                    scope_invalid_review_rows += 1
                if "low_exact_ksa_overlap" in flags or "low_exact_high_adjusted" in scope_flags:
                    aggregate["rows_with_low_exact_flag"] += 1
                    major_counts["rows_with_low_exact_flag"] += 1
                if "baseline_heavy" in flags or "baseline_heavy_pair_ratio_ge_0.2" in scope_flags:
                    aggregate["rows_with_baseline_heavy_flag"] += 1
                    major_counts["rows_with_baseline_heavy_flag"] += 1
                if _review_status_is_unsafe(review_status):
                    aggregate["unsafe_review_status_count"] += 1
                    major_counts["unsafe_review_status_count"] += 1
                    scope_unsafe_rows += 1

            priority_score = (
                scope_review_rows
                + scope_without_course_links * 2
                + (2 if "baseline_heavy_pair_ratio_ge_0.2" in scope_flags else 0)
                + (2 if "low_exact_high_adjusted" in scope_flags else 0)
                + scope_unsafe_rows * 3
                + scope_invalid_review_rows * 3
            )
            if priority_score:
                priority_scopes.append(
                    {
                        "major_code": result.get("major_code"),
                        "major_name": result.get("major_name"),
                        "scope_label": scope.get("scope_label"),
                        "priority_score": priority_score,
                        "matrix_row_count": len(matrix),
                        "rows_requiring_human_review": scope_review_rows,
                        "rows_without_course_links": scope_without_course_links,
                        "unsafe_review_status_count": scope_unsafe_rows,
                        "invalid_review_status_count": scope_invalid_review_rows,
                        "review_flags": sorted(scope_flags),
                        "avg_adjusted": _score_value(score, "avg_adjusted"),
                        "avg_exact": _score_value(score, "avg_exact"),
                        "baseline_heavy_pair_ratio": _score_value(score, "baseline_heavy_pair_ratio"),
                        "top_hub_units": _scope_top_hubs(scope, limit=5),
                    }
                )

        matrix_rows = int(major_counts["matrix_row_count"] or 0)
        major_rows.append(
            {
                "major_code": result.get("major_code"),
                "major_name": result.get("major_name"),
                "ok": True,
                **major_counts,
                "course_link_row_coverage": round(
                    float(major_counts["matrix_rows_with_course_links"] or 0) / matrix_rows,
                    4,
                )
                if matrix_rows
                else 0.0,
            }
        )

    matrix_row_count = int(aggregate["matrix_row_count"] or 0)
    aggregate["course_link_row_coverage"] = (
        round(float(aggregate["matrix_rows_with_course_links"] or 0) / matrix_row_count, 4)
        if matrix_row_count
        else 0.0
    )
    aggregate["human_review_required"] = int(aggregate["rows_requiring_human_review"] or 0) > 0
    aggregate["approval_claim"] = False
    aggregate["db_writes"] = False
    aggregate["guide_role"] = "framework_reference"

    priority_scopes.sort(
        key=lambda item: (
            -int(item.get("priority_score") or 0),
            -int(item.get("rows_without_course_links") or 0),
            -float(item.get("baseline_heavy_pair_ratio") or 0.0),
            _clean(item.get("major_code")),
            _clean(item.get("scope_label")),
        )
    )
    major_rows.sort(key=lambda item: _clean(item.get("major_code")))
    review_major_rows = sorted(
        [row for row in major_rows if row.get("ok") is not False],
        key=lambda row: (
            -int(row.get("rows_requiring_human_review") or 0),
            -int(row.get("matrix_rows_without_course_links") or 0),
            float(row.get("course_link_row_coverage") or 0.0),
            _clean(row.get("major_code")),
        ),
    )

    if aggregate["unsafe_review_status_count"]:
        findings.append(
            {
                "severity": "blocker",
                "code": "unsafe_review_status_present",
                "message": "Automated education-system artifacts contain review statuses that look approved or reviewed.",
                "count": aggregate["unsafe_review_status_count"],
            }
        )
    if aggregate["invalid_review_status_count"]:
        findings.append(
            {
                "severity": "blocker",
                "code": "invalid_human_review_status",
                "message": "Every automated education-system matrix row must remain explicitly needs_review.",
                "count": aggregate["invalid_review_status_count"],
            }
        )
    if aggregate["matrix_rows_without_course_links"]:
        findings.append(
            {
                "severity": "review",
                "code": "matrix_rows_without_course_links",
                "message": "Some education-system rows lack direct training-course links and need course-link review.",
                "count": aggregate["matrix_rows_without_course_links"],
            }
        )
    if aggregate["rows_with_baseline_heavy_flag"] or aggregate["rows_with_low_exact_flag"]:
        findings.append(
            {
                "severity": "review",
                "code": "ontology_adjustment_caveat_rows",
                "message": "Some rows depend heavily on ontology adjustment or have low exact KSA overlap.",
                "baseline_heavy_rows": aggregate["rows_with_baseline_heavy_flag"],
                "low_exact_rows": aggregate["rows_with_low_exact_flag"],
            }
        )
    contract_ok = (
        not load_errors
        and int(aggregate["unsafe_review_status_count"] or 0) == 0
        and int(aggregate["invalid_review_status_count"] or 0) == 0
        and matrix_row_count > 0
    )
    approval_ready = contract_ok and not bool(aggregate["human_review_required"])
    status = (
        "approval_ready"
        if approval_ready
        else "review_required"
        if contract_ok
        else "contract_failed"
    )

    return {
        "schema": EDUCATION_SYSTEM_AUDIT_SCHEMA_VERSION,
        "generated_at": _now(),
        "source_run": _portable_path(major_run_path),
        "source_run_resolved": _portable_path(major_run_path),
        "source_run_fingerprint": source_run_fingerprint,
        "ok": approval_ready,
        "contract_ok": contract_ok,
        "approval_ready": approval_ready,
        "status": status,
        "source_run_schema": run.get("schema"),
        "failed_major_count": len(load_errors),
        "load_errors": load_errors,
        "aggregate": aggregate,
        "major_rows": major_rows,
        "priority_major_rows": review_major_rows[:10],
        "priority_scopes": priority_scopes[:25],
        "findings": findings,
        "finding_count": len(findings),
        "guide_alignment": {
            "C1-1": {
                "surface": "scope_confirmation",
                "evidence": "recommended_path guide_stage C1-1 and scope/unit coverage",
                "stage_count": (aggregate.get("guide_stage_counts") or {}).get("C1-1", 0),
            },
            "C1-2": {
                "surface": "training necessity / required-optional review",
                "evidence": "training_system_matrix required_optional_basis and human_review",
                "row_count": aggregate.get("matrix_row_count"),
                "human_review_required_rows": aggregate.get("rows_requiring_human_review"),
            },
            "C2-1": {
                "surface": "education training system matrix",
                "evidence": "training_system_matrix grouped by job scope, level, education type, delivery, course fit",
                "row_count": aggregate.get("matrix_row_count"),
            },
            "C2-2": {
                "surface": "annual operation planning readiness",
                "evidence": "delivery_operation, course_fit.hours/methods/facilities, facility_constraint_fit",
                "delivery_rows": aggregate.get("matrix_row_count"),
                "facility_fit_counts": aggregate.get("facility_fit_counts"),
            },
        },
        "review_gate": {
            "status": "open" if aggregate["human_review_required"] else "closed",
            "approval_claim": False,
            "approval_ready": approval_ready,
            "human_review_required": aggregate["human_review_required"],
            "rows_requiring_human_review": aggregate["rows_requiring_human_review"],
        },
        "non_mutation_note": (
            "Report-only audit. No DB rows, human-review statuses, source KSA text, "
            "or ontology definitions were modified."
        ),
        "recommended_next_actions": [
            "Review priority_scopes before changing ontology-adjusted score thresholds.",
            "Run course-link gap diagnostics for majors with many matrix_rows_without_course_links.",
            "Use public curriculum or official NCS evidence to validate baseline-heavy and low-exact rows.",
        ],
    }


def _course_name_exact_count(conn: sqlite3.Connection, unit_name: str) -> int:
    if not unit_name:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM ncs_training_courses WHERE compe_unit_name = ?",
        (unit_name,),
    ).fetchone()
    if row is None:
        return 0
    return int(row["count"] if isinstance(row, sqlite3.Row) else row[0])


def _course_name_similar_hits(
    conn: sqlite3.Connection,
    unit_name: str,
    *,
    limit: int,
) -> list[str]:
    normalized = _clean(unit_name)
    if len(normalized) < 4 or limit <= 0:
        return []
    prefix = normalized[:12]
    rows = conn.execute(
        """
        SELECT DISTINCT compe_unit_name
        FROM ncs_training_courses
        WHERE compe_unit_name LIKE ?
        ORDER BY compe_unit_name
        LIMIT ?
        """,
        (f"%{prefix}%", limit),
    ).fetchall()
    hits: list[str] = []
    for row in rows:
        name = _clean(row["compe_unit_name"] if isinstance(row, sqlite3.Row) else row[0])
        if name and name != normalized and name not in hits:
            hits.append(name)
    return hits


def build_ontology_transferability_course_link_gap_diagnostic(
    conn: sqlite3.Connection,
    major_run_path: Path,
    *,
    sample_units: int = 10,
    similar_course_limit: int = 3,
) -> dict[str, Any]:
    run, loaded = _load_major_transferability_reports(major_run_path)
    scopes: list[dict[str, Any]] = []
    load_errors: list[dict[str, Any]] = []
    issue_counts: dict[str, int] = defaultdict(int)
    major_counts: dict[tuple[str, str, str], int] = defaultdict(int)

    for item in loaded:
        result = item["run_result"]
        major_code = _clean(result.get("major_code"))
        major_name = _clean(result.get("major_name"))
        report = item.get("report")
        if item.get("load_error"):
            load_errors.append(
                {
                    "major_code": major_code,
                    "major_name": major_name,
                    "artifact_path": str(item.get("artifact_path")),
                    "error": item.get("load_error"),
                }
            )
            continue
        if not report:
            continue
        for scope in report.get("scopes") or []:
            matrix = (scope.get("education_system") or {}).get("training_system_matrix") or []
            if not matrix:
                continue
            linked_rows = [
                row
                for row in matrix
                if int(((row.get("course_link") or {}).get("linked_training_course_count") or 0)) > 0
            ]
            unlinked_rows = [
                row
                for row in matrix
                if int(((row.get("course_link") or {}).get("linked_training_course_count") or 0)) <= 0
            ]
            if not unlinked_rows:
                continue
            partial_gap = bool(linked_rows)

            exact_hits: list[dict[str, Any]] = []
            similar_hits: list[dict[str, Any]] = []
            cross_scope_hits: list[dict[str, Any]] = []
            units: list[dict[str, str]] = []
            for row in unlinked_rows[: max(1, sample_units)]:
                unit_name = _clean(row.get("unit_name"))
                unit_code = _clean(row.get("unit_code"))
                units.append({"unit_code": unit_code, "unit_name": unit_name})
                exact_courses = _annotate_courses_with_scope_fit(
                    _training_course_candidates_for_unit(
                        conn,
                        unit_name,
                        candidate_type="unit_name_exact",
                        limit=max(1, sample_units),
                    ),
                    scope,
                )
                exact_link_candidates = [
                    course for course in exact_courses if _course_is_scope_link_candidate(course)
                ]
                exact_reference_only = [
                    course for course in exact_courses if not _course_is_scope_link_candidate(course)
                ]
                if exact_link_candidates:
                    exact_hits.append(
                        {
                            "unit_name": unit_name,
                            "course_count": len(exact_link_candidates),
                            "scope_fit_status_counts": _course_scope_fit_status_counts(
                                exact_link_candidates
                            ),
                        }
                    )
                if exact_reference_only:
                    cross_scope_hits.append(
                        {
                            "unit_name": unit_name,
                            "match_type": "unit_name_exact",
                            "course_count": len(exact_reference_only),
                            "course_names": [
                                course.get("compe_unit_name") or ""
                                for course in exact_reference_only[: max(1, similar_course_limit)]
                            ],
                            "scope_fit_status_counts": _course_scope_fit_status_counts(
                                exact_reference_only
                            ),
                        }
                    )

                similar_courses = _annotate_courses_with_scope_fit(
                    _training_course_candidates_for_unit(
                        conn,
                        unit_name,
                        candidate_type="unit_name_similar",
                        limit=max(0, similar_course_limit),
                    ),
                    scope,
                )
                for course in similar_courses:
                    course_name = course.get("compe_unit_name") or ""
                    if _course_is_scope_link_candidate(course):
                        similar_hits.append(
                            {
                                "unit_name": unit_name,
                                "course_name": course_name,
                                "scope_fit_status": (course.get("scope_fit") or {}).get("status"),
                            }
                        )
                    else:
                        cross_scope_hits.append(
                            {
                                "unit_name": unit_name,
                                "match_type": "unit_name_similar",
                                "course_count": 1,
                                "course_names": [course_name],
                                "scope_fit_status_counts": _course_scope_fit_status_counts([course]),
                            }
                        )

            if exact_hits:
                issue_type = "possible_unit_link_gap"
            elif similar_hits:
                issue_type = "possible_name_normalization_gap"
            elif cross_scope_hits:
                issue_type = "cross_scope_name_only"
            else:
                issue_type = "likely_no_training_course_rows"
            issue_counts[issue_type] += 1
            major_counts[(major_code, major_name, issue_type)] += 1

            flags = _scope_review_flags(scope)
            if partial_gap:
                if "partial_training_course_link_gap" not in flags:
                    flags.append("partial_training_course_link_gap")
            elif "no_direct_training_course_links_in_matrix" not in flags:
                flags.append("no_direct_training_course_links_in_matrix")
            scopes.append(
                {
                    "major_code": major_code,
                    "major_name": major_name,
                    "scope_label": scope.get("scope_label"),
                    "classification": scope.get("classification"),
                    "unit_count": len(matrix),
                    "unlinked_unit_count": len(unlinked_rows),
                    "linked_unit_count": len(linked_rows),
                    "flags": flags,
                    "score_summary": scope.get("score_summary"),
                    "issue_type": issue_type,
                    "exact_course_name_hits": exact_hits[: max(1, sample_units)],
                    "similar_course_name_hits": similar_hits[: max(1, sample_units)],
                    "cross_scope_course_name_hits": cross_scope_hits[: max(1, sample_units)],
                    "sample_units": units,
                }
            )

    contract_ok = not load_errors and bool(run.get("ok"))
    human_review_required = bool(scopes)
    return {
        "schema": COURSE_LINK_GAP_DIAGNOSTIC_SCHEMA_VERSION,
        "generated_at": _now(),
        "source_run": str(major_run_path),
        "source_run_fingerprint": _file_fingerprint(major_run_path),
        "ok": contract_ok,
        "contract_ok": contract_ok,
        "approval_ready": False,
        "status": (
            "review_required"
            if contract_ok and human_review_required
            else "no_gaps_detected"
            if contract_ok
            else "contract_failed"
        ),
        "human_review_required": human_review_required,
        "approval_claim": False,
        "db_writes": False,
        "review_gate": {
            "status": "open" if human_review_required else "closed",
            "approval_claim": False,
            "approval_ready": False,
            "human_review_required": human_review_required,
        },
        "failed_major_count": sum(
            1 for result in run.get("results") or [] if int(result.get("returncode") or 0) != 0
        ),
        "load_errors": load_errors,
        "scope_count": len(scopes),
        "issue_type_counts": dict(sorted(issue_counts.items())),
        "major_issue_counts": [
            {
                "major_code": major_code,
                "major_name": major_name,
                "issue_type": issue_type,
                "scope_count": count,
            }
            for (major_code, major_name, issue_type), count in sorted(major_counts.items())
        ],
        "scopes": scopes,
        "non_mutation_note": (
            "Report-only diagnostic. No course links, review statuses, source KSA text, "
            "or ontology definitions were modified."
        ),
    }


def _training_course_select_expr(conn: sqlite3.Connection) -> str:
    rows = conn.execute("PRAGMA table_info(ncs_training_courses)").fetchall()
    available = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in rows}
    fields = [
        "training_course_id",
        "ncs_cl_cd",
        "compe_unit_name",
        "compe_unit_level",
        "ncs_lclas_cd",
        "ncs_lclas_cdnm",
        "ncs_mclas_cd",
        "ncs_mclas_cdnm",
        "ncs_sclas_cd",
        "ncs_sclas_cdnm",
        "ncs_subd_cd",
        "ncs_subd_cdnm",
        "train_goal",
        "train_time",
        "fac_name",
        "meth_name",
    ]
    return ", ".join(field if field in available else f"NULL AS {field}" for field in fields)


def _course_candidate_payload(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    def value(key: str) -> Any:
        if isinstance(row, sqlite3.Row):
            return row[key]
        return None

    goal = _clean(value("train_goal"))
    return {
        "training_course_id": value("training_course_id"),
        "ncs_cl_cd": _clean(value("ncs_cl_cd")),
        "compe_unit_name": _clean(value("compe_unit_name")),
        "compe_unit_level": _clean(value("compe_unit_level")),
        "classification": {
            "major_code": _clean(value("ncs_lclas_cd")),
            "major_name": _clean(value("ncs_lclas_cdnm")),
            "middle_code": _clean(value("ncs_mclas_cd")),
            "middle_name": _clean(value("ncs_mclas_cdnm")),
            "small_code": _clean(value("ncs_sclas_cd")),
            "small_name": _clean(value("ncs_sclas_cdnm")),
            "sub_code": _clean(value("ncs_subd_cd")),
            "sub_name": _clean(value("ncs_subd_cdnm")),
        },
        "train_time": _clean(value("train_time")),
        "meth_name": _clean(value("meth_name")),
        "fac_name": _clean(value("fac_name")),
        "train_goal_preview": goal[:300],
    }


def _classification_path(classification: dict[str, Any]) -> str:
    return " > ".join(
        part
        for part in [
            _clean(classification.get("major_name") or classification.get("major_code")),
            _clean(classification.get("middle_name") or classification.get("middle_code")),
            _clean(classification.get("small_name") or classification.get("small_code")),
            _clean(classification.get("sub_name") or classification.get("sub_code")),
        ]
        if part
    )


def _course_scope_fit(course: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    scope_class = scope.get("classification") or {}
    course_class = course.get("classification") or {}
    scope_major = _clean(scope_class.get("major_code") or scope.get("major_code"))
    course_major = _clean(course_class.get("major_code"))
    scope_middle = _clean(scope_class.get("middle_code"))
    course_middle = _clean(course_class.get("middle_code"))
    scope_small = _clean(scope_class.get("small_code"))
    course_small = _clean(course_class.get("small_code"))
    scope_sub = _clean(scope_class.get("sub_code"))
    course_sub = _clean(course_class.get("sub_code"))
    warnings: list[str] = []
    if not course_major:
        status = "unknown_course_scope"
        warnings.append("course_classification_missing")
    elif scope_major and course_major != scope_major:
        status = "off_scope_major"
        warnings.append("course_major_differs_from_target_scope")
    elif scope_sub and course_sub and (scope_middle, scope_small, scope_sub) == (
        course_middle,
        course_small,
        course_sub,
    ):
        status = "same_sub_classification"
    elif scope_small and course_small and (scope_middle, scope_small) == (course_middle, course_small):
        status = "same_small_classification"
    elif scope_middle and course_middle and scope_middle == course_middle:
        status = "same_middle_classification"
    else:
        status = "same_major_only"
        warnings.append("course_scope_is_broader_or_adjacent")
    return {
        "status": status,
        "scope_major_code": scope_major,
        "course_major_code": course_major,
        "target_scope_path": _classification_path(scope_class),
        "course_scope_path": _classification_path(course_class),
        "warnings": warnings,
    }


def _annotate_courses_with_scope_fit(
    courses: list[dict[str, Any]],
    scope: dict[str, Any],
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for course in courses:
        item = dict(course)
        item["scope_fit"] = _course_scope_fit(item, scope)
        annotated.append(item)
    return annotated


def _course_is_scope_link_candidate(course: dict[str, Any]) -> bool:
    return (course.get("scope_fit") or {}).get("status") in COURSE_SCOPE_LINK_CANDIDATE_STATUSES


def _course_scope_fit_status_counts(courses: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for course in courses:
        _increment(counts, (course.get("scope_fit") or {}).get("status"))
    return dict(sorted(counts.items()))


def _training_course_candidates_for_unit(
    conn: sqlite3.Connection,
    unit_name: str,
    *,
    candidate_type: str,
    limit: int,
) -> list[dict[str, Any]]:
    normalized = _clean(unit_name)
    if not normalized or limit <= 0:
        return []
    select_expr = _training_course_select_expr(conn)
    if candidate_type == "unit_name_exact":
        rows = conn.execute(
            f"""
            SELECT {select_expr}
            FROM ncs_training_courses
            WHERE compe_unit_name = ?
            ORDER BY training_course_id
            LIMIT ?
            """,
            (normalized, limit),
        ).fetchall()
    elif candidate_type == "unit_name_similar":
        if len(normalized) < 4:
            return []
        prefix = normalized[:12]
        rows = conn.execute(
            f"""
            SELECT {select_expr}
            FROM ncs_training_courses
            WHERE compe_unit_name LIKE ?
              AND compe_unit_name <> ?
            ORDER BY compe_unit_name, training_course_id
            LIMIT ?
            """,
            (f"%{prefix}%", normalized, limit),
        ).fetchall()
    else:
        return []
    return [_course_candidate_payload(row) for row in rows]


def build_ontology_transferability_course_link_candidate_review(
    conn: sqlite3.Connection,
    gap_diagnostic_path: Path,
    *,
    course_limit: int = 5,
    include_issue_types: list[str] | None = None,
) -> dict[str, Any]:
    diagnostic = _load_json(gap_diagnostic_path)
    validation_issues: list[dict[str, Any]] = []
    if diagnostic.get("schema") != COURSE_LINK_GAP_DIAGNOSTIC_SCHEMA_VERSION:
        validation_issues.append(
            {
                "code": "gap_diagnostic_schema",
                "message": "Course-link gap diagnostic schema is invalid.",
                "expected": COURSE_LINK_GAP_DIAGNOSTIC_SCHEMA_VERSION,
                "actual": diagnostic.get("schema"),
            }
        )
    issue_types = set(
        include_issue_types
        or ["possible_unit_link_gap", "possible_name_normalization_gap"]
    )
    candidate_type_counts: dict[str, int] = defaultdict(int)
    issue_type_counts: dict[str, int] = defaultdict(int)
    scope_fit_status_counts: dict[str, int] = defaultdict(int)
    scopes: list[dict[str, Any]] = []
    unit_candidate_count = 0
    course_candidate_count = 0

    for scope_index, scope in enumerate(diagnostic.get("scopes") or [], start=1):
        issue_type = _clean(scope.get("issue_type"))
        if issue_type not in issue_types:
            continue
        unit_candidates: list[dict[str, Any]] = []
        for unit in scope.get("sample_units") or []:
            unit_name = _clean(unit.get("unit_name"))
            if not unit_name:
                continue
            candidate_groups: list[dict[str, Any]] = []
            exact_courses = _training_course_candidates_for_unit(
                conn,
                unit_name,
                candidate_type="unit_name_exact",
                limit=max(1, course_limit),
            )
            exact_courses = _annotate_courses_with_scope_fit(exact_courses, scope)
            exact_courses = [
                course for course in exact_courses if _course_is_scope_link_candidate(course)
            ]
            if exact_courses:
                candidate_groups.append(
                    {
                        "candidate_type": "unit_name_exact",
                        "evidence_reason": (
                            "Training API contains course rows with the same competency-unit name, "
                            "but the education-system matrix has no direct training-course link."
                        ),
                        "courses": exact_courses,
                    }
                )
                candidate_type_counts["unit_name_exact"] += 1
                course_candidate_count += len(exact_courses)
                for course in exact_courses:
                    _increment(scope_fit_status_counts, (course.get("scope_fit") or {}).get("status"))
            similar_courses = _training_course_candidates_for_unit(
                conn,
                unit_name,
                candidate_type="unit_name_similar",
                limit=max(1, course_limit),
            )
            similar_courses = _annotate_courses_with_scope_fit(similar_courses, scope)
            similar_courses = [
                course for course in similar_courses if _course_is_scope_link_candidate(course)
            ]
            if similar_courses:
                candidate_groups.append(
                    {
                        "candidate_type": "unit_name_similar",
                        "evidence_reason": (
                            "Training API contains similarly named course rows; name normalization "
                            "or classification mapping may need review."
                        ),
                        "courses": similar_courses,
                    }
                )
                candidate_type_counts["unit_name_similar"] += 1
                course_candidate_count += len(similar_courses)
                for course in similar_courses:
                    _increment(scope_fit_status_counts, (course.get("scope_fit") or {}).get("status"))
            if candidate_groups:
                unit_candidate_count += 1
                unit_candidates.append(
                    {
                        "unit_code": _clean(unit.get("unit_code")),
                        "unit_name": unit_name,
                        "candidate_groups": candidate_groups,
                    }
                )
        if not unit_candidates:
            continue
        issue_type_counts[issue_type] += 1
        scopes.append(
            {
                "source_scope_sequence": scope_index,
                "major_code": scope.get("major_code"),
                "major_name": scope.get("major_name"),
                "scope_label": scope.get("scope_label"),
                "issue_type": issue_type,
                "score_summary": scope.get("score_summary"),
                "flags": scope.get("flags") or [],
                "unit_candidates": unit_candidates,
                "review_action": "human_review_required_before_linking",
            }
        )

    contract_ok = not validation_issues and bool(diagnostic.get("contract_ok", diagnostic.get("ok")))
    human_review_required = bool(scopes)
    return {
        "schema": COURSE_LINK_CANDIDATE_REVIEW_SCHEMA_VERSION,
        "generated_at": _now(),
        "gap_diagnostic": str(gap_diagnostic_path),
        "gap_diagnostic_fingerprint": _file_fingerprint(gap_diagnostic_path),
        "source_run": diagnostic.get("source_run"),
        "source_run_fingerprint": diagnostic.get("source_run_fingerprint"),
        "ok": contract_ok,
        "contract_ok": contract_ok,
        "approval_ready": False,
        "status": (
            "review_required"
            if contract_ok and human_review_required
            else "no_candidates"
            if contract_ok
            else "contract_failed"
        ),
        "human_review_required": human_review_required,
        "approval_claim": False,
        "db_writes": False,
        "review_gate": {
            "status": "open" if human_review_required else "closed",
            "approval_claim": False,
            "approval_ready": False,
            "human_review_required": human_review_required,
        },
        "validation_issues": validation_issues,
        "scope_count": len(scopes),
        "unit_candidate_count": unit_candidate_count,
        "course_candidate_count": course_candidate_count,
        "issue_type_counts": dict(sorted(issue_type_counts.items())),
        "candidate_type_counts": dict(sorted(candidate_type_counts.items())),
        "scope_fit_status_counts": dict(sorted(scope_fit_status_counts.items())),
        "scopes": scopes,
        "non_mutation_note": (
            "Report-only candidate review. No training-course links, review statuses, "
            "source KSA text, or ontology definitions were modified."
        ),
    }


def _evidence_review_flags(
    *,
    avg_exact: float,
    baseline_dependency_ratio: float,
    linked_training_course_count: int,
) -> list[str]:
    flags: list[str] = []
    if avg_exact <= 0.03:
        flags.append("low_exact_ksa_overlap")
    if baseline_dependency_ratio >= 0.9:
        flags.append("baseline_heavy")
    if linked_training_course_count <= 0:
        flags.append("no_direct_training_course_link")
    return flags


def _education_group(
    avg_adjusted: float,
    *,
    avg_exact: float,
    baseline_dependency_ratio: float,
    linked_training_course_count: int,
    rank: int,
    total: int,
    has_absolute_core: bool,
) -> dict[str, Any]:
    relative_core_cutoff = max(1, math.ceil(total * 0.2))
    evidence_flags = _evidence_review_flags(
        avg_exact=avg_exact,
        baseline_dependency_ratio=baseline_dependency_ratio,
        linked_training_course_count=linked_training_course_count,
    )
    has_required_evidence = not evidence_flags
    nonrequired_blocking_flags = [
        flag for flag in evidence_flags if flag == "no_direct_training_course_link"
    ]
    caveat_suffix = "_with_caveats_" + ",".join(evidence_flags) if evidence_flags else ""
    if avg_adjusted >= 0.34:
        required_optional = "required" if has_required_evidence else (
            "review" if nonrequired_blocking_flags else "recommended"
        )
        return {
            "code": "common_transfer_hub",
            "label": "Common transfer hub",
            "required_optional": required_optional,
            "basis": (
                "average_adjusted_ge_0.34_with_exact_ksa_and_course_link"
                if has_required_evidence
                else (
                    "average_adjusted_ge_0.34"
                    + caveat_suffix
                    if not nonrequired_blocking_flags
                    else "average_adjusted_ge_0.34_but_review_required_for_"
                    + ",".join(nonrequired_blocking_flags)
                )
            ),
            "evidence_flags": evidence_flags,
        }
    if not has_absolute_core and rank <= relative_core_cutoff:
        return {
            "code": "common_transfer_hub",
            "label": "Common transfer hub",
            "required_optional": "recommended" if not nonrequired_blocking_flags else "review",
            "basis": (
                "relative_top_20_percent_no_absolute_core_with_course_link"
                + caveat_suffix
                if not nonrequired_blocking_flags
                else "relative_top_20_percent_no_absolute_core_but_review_required_for_"
                + ",".join(nonrequired_blocking_flags)
            ),
            "evidence_flags": evidence_flags,
        }
    if avg_adjusted >= 0.30:
        return {
            "code": "adjacent_expansion",
            "label": "Adjacent expansion",
            "required_optional": "recommended" if not nonrequired_blocking_flags else "review",
            "basis": (
                "average_adjusted_ge_0.30"
                + caveat_suffix
                if not nonrequired_blocking_flags
                else "average_adjusted_ge_0.30_but_review_required_for_"
                + ",".join(nonrequired_blocking_flags)
            ),
            "evidence_flags": evidence_flags,
        }
    if avg_adjusted >= 0.26:
        return {
            "code": "specialized_supplement",
            "label": "Specialized supplement",
            "required_optional": "optional" if not nonrequired_blocking_flags else "review",
            "basis": (
                "average_adjusted_ge_0.26"
                + caveat_suffix
                if not nonrequired_blocking_flags
                else "average_adjusted_ge_0.26_but_review_required_for_"
                + ",".join(nonrequired_blocking_flags)
            ),
            "evidence_flags": evidence_flags,
        }
    return {
        "code": "human_review_low_evidence",
        "label": "Human review low-evidence",
        "required_optional": "review",
        "basis": "average_adjusted_lt_0.26",
        "evidence_flags": evidence_flags,
    }


def _matrix_row(
    *,
    scope: dict[str, Any],
    unit: dict[str, Any],
    summary: dict[str, Any],
    group: dict[str, Any],
    course_link: dict[str, Any],
) -> dict[str, Any]:
    level_band = _target_level_band(unit.get("unit_level_raw"))
    evidence_flags = list(group.get("evidence_flags") or [])
    return {
        "job_scope": {
            "major": scope.get("major_name"),
            "middle": scope.get("middle_name"),
            "small": scope.get("small_name"),
            "sub": scope.get("sub_name"),
            "classification_id": scope.get("classification_id"),
        },
        "unit_code": unit.get("unit_code"),
        "unit_name": _unit_label(unit),
        "target_level_band": level_band,
        "education_type": {
            "code": group["code"],
            "label": group["label"],
        },
        "required_optional_basis": {
            "code": group["required_optional"],
            "basis": group["basis"],
        },
        "delivery_operation": {
            "code": "course_linked" if course_link["linked_training_course_count"] else "unit_standard_only",
            "methods": course_link["methods"],
            "facilities": course_link["facilities"],
        },
        "planner_grouping": {
            "scope": "same_sub_classification",
            "education_type": group["code"],
            "required_optional": group["required_optional"],
            "target_level_band": level_band["code"],
        },
        "task_ksa_basis": {
            "average_adjusted_transferability": summary["avg_adjusted"],
            "average_exact_ksa_overlap": summary["avg_exact"],
            "average_adjusted_minus_exact": summary["avg_adjusted_minus_exact"],
            "baseline_dependency_ratio": summary["baseline_dependency_ratio"],
            "top_target_units": summary["top_target_units"],
            "basis_types": [
                "exact_ksa_overlap",
                "ontology_related_concepts",
                "task_similarity",
                "same_sub_classification_scope",
            ],
        },
        "facility_constraint_fit": {
            "status": "not_requested",
            "requested": [],
            "available": course_link["facilities"],
            "matched": [],
            "missing": [],
            "rationale": "Batch education-system profile did not request facility constraints.",
        },
        "human_review": {
            "status": "needs_review",
            "action": "review_training_system_row",
            "rationale": (
                "Automated ontology-adjusted grouping requires human confirmation before adoption."
                if not evidence_flags
                else (
                    "Grouping is review-gated because one or more required evidence checks are weak."
                    if group["required_optional"] == "review"
                    else "Non-required grouping carries evidence caveats and needs human confirmation."
                )
            ),
            "flags": evidence_flags,
        },
        "course_link": {
            "linked_training_course_count": course_link["linked_training_course_count"],
            "sample_course_names": course_link["sample_course_names"],
            "mapping_chain": ["classification", "competency_unit", "KSA", "training_course"],
        },
        "course_fit": {
            "level": unit.get("unit_level_raw"),
            "hours": course_link["hours"],
            "methods": course_link["methods"],
            "facilities": course_link["facilities"],
        },
    }


def _scope_recommended_path(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    def selected_items(
        *codes: str,
        allowed_required_optional: set[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for code in codes:
            for item in groups.get(code, []):
                group = item.get("education_group") or {}
                required_optional = _clean(group.get("required_optional"))
                if allowed_required_optional is not None and required_optional not in allowed_required_optional:
                    continue
                items.append(item)
        return items[:limit] if limit is not None else items

    def names(items: list[dict[str, Any]]) -> list[str]:
        return [item["unit_name"] for item in items]

    def unit_codes(items: list[dict[str, Any]]) -> list[str]:
        return [item["unit_code"] for item in items if item.get("unit_code")]

    scope_items = selected_items(
        "common_transfer_hub",
        "adjacent_expansion",
        "specialized_supplement",
        "human_review_low_evidence",
        limit=12,
    )
    core_items = selected_items(
        "common_transfer_hub",
        allowed_required_optional={"required"},
    )
    supporting_items = selected_items(
        "common_transfer_hub",
        "adjacent_expansion",
        "specialized_supplement",
        allowed_required_optional={"recommended", "optional"},
    )
    review_items = selected_items(
        "common_transfer_hub",
        "adjacent_expansion",
        "specialized_supplement",
        "human_review_low_evidence",
        allowed_required_optional={"review"},
    )

    return [
        {
            "stage": 1,
            "role": "scope_confirmation",
            "title": "Confirm NCS sub-classification and unit coverage",
            "units": names(scope_items),
            "unit_codes": unit_codes(scope_items),
            "guide_stage": "C1-1",
            "guide_stage_status": "needs_review",
            "guide_stage_evidence": {
                "scope_basis": "same_sub_classification",
                "unit_count": sum(len(items) for items in groups.values()),
            },
        },
        {
            "stage": 2,
            "role": "core_gap_training",
            "title": "Build required common transfer hub first",
            "units": names(core_items),
            "unit_codes": unit_codes(core_items),
            "guide_stage": "C1-2",
            "guide_stage_status": "needs_review",
            "guide_stage_evidence": {
                "group": "common_transfer_hub",
                "required_optional": "required",
                "selection_basis": "required common-transfer rows with exact KSA and course-link support",
            },
        },
        {
            "stage": 3,
            "role": "supporting_or_adjacent_training",
            "title": "Add recommended and optional support",
            "units": names(supporting_items),
            "unit_codes": unit_codes(supporting_items),
            "guide_stage": "C2-1",
            "guide_stage_status": "needs_review",
            "guide_stage_evidence": {
                "groups": ["common_transfer_hub", "adjacent_expansion", "specialized_supplement"],
                "required_optional": ["recommended", "optional"],
                "selection_basis": "non-required rows with direct course links and same-scope relevance",
            },
        },
        {
            "stage": 4,
            "role": "delivery_fit_review",
            "title": "Review low-evidence, role-sensitive, and delivery-fit units",
            "units": names(review_items),
            "unit_codes": unit_codes(review_items),
            "guide_stage": "C2-2",
            "guide_stage_status": "needs_review",
            "guide_stage_evidence": {
                "groups": [
                    "common_transfer_hub",
                    "adjacent_expansion",
                    "specialized_supplement",
                    "human_review_low_evidence",
                ],
                "required_optional": "review",
                "review_basis": "review-gated evidence, course links, methods, hours, and facilities",
            },
        },
    ]


def build_ontology_adjusted_education_systems(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    limit_scopes: int | None = None,
    min_units: int = 2,
    max_units_per_scope: int | None = None,
    top_pairs: int = 10,
    top_units: int = 12,
) -> dict[str, Any]:
    scopes = _fetch_scopes(
        conn,
        major_code=major_code,
        limit_scopes=limit_scopes,
        min_units=max(2, min_units),
    )
    scope_ids = {int(scope["classification_id"]) for scope in scopes}
    units_by_scope = _fetch_units_by_scope(conn, scope_ids)
    all_unit_codes = {
        _clean(unit.get("unit_code"))
        for units in units_by_scope.values()
        for unit in units
        if _clean(unit.get("unit_code"))
    }
    concept_by_unit = _load_unit_concepts(conn, all_unit_codes)
    job_base_by_unit = _load_job_base_keys(conn, all_unit_codes)
    qualification_by_unit = _load_qualification_keys(conn, all_unit_codes)
    course_links_by_unit = _load_course_links(conn, all_unit_codes)
    all_concept_ids = {concept_id for concepts in concept_by_unit.values() for concept_id in concepts}
    relation_adjacency = _load_relation_adjacency(conn, all_concept_ids)

    report_scopes: list[dict[str, Any]] = []
    all_pair_rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    directed_count = 0
    for scope in scopes:
        raw_units = list(units_by_scope.get(int(scope["classification_id"]), []))
        if max_units_per_scope is not None and len(raw_units) > max_units_per_scope:
            skipped.append(
                {
                    "classification_id": scope["classification_id"],
                    "label": _scope_label(scope),
                    "unit_count": len(raw_units),
                    "reason": "max_units_per_scope_exceeded",
                }
            )
            continue
        unit_codes = {_clean(unit.get("unit_code")) for unit in raw_units if _clean(unit.get("unit_code"))}
        task_similarity = _load_task_similarity_for_scope(conn, unit_codes)
        directed: list[dict[str, Any]] = []
        for source in raw_units:
            source_code = _clean(source.get("unit_code"))
            for target in raw_units:
                target_code = _clean(target.get("unit_code"))
                if not source_code or not target_code or source_code == target_code:
                    continue
                fit = _semantic_fit(
                    source_unit=source_code,
                    target_unit=target_code,
                    source_ids=concept_by_unit.get(source_code, set()),
                    target_ids=concept_by_unit.get(target_code, set()),
                    source_job_base=job_base_by_unit.get(source_code, set()),
                    target_job_base=job_base_by_unit.get(target_code, set()),
                    source_qualifications=qualification_by_unit.get(source_code, set()),
                    target_qualifications=qualification_by_unit.get(target_code, set()),
                    adjacency=relation_adjacency,
                    task_similarity=task_similarity,
                )
                fit.update(
                    {
                        "source_unit_name": _unit_label(source),
                        "target_unit_name": _unit_label(target),
                        "scope_classification_id": scope["classification_id"],
                        "scope_label": _scope_label(scope),
                    }
                )
                directed.append(fit)
        directed_count += len(directed)
        all_pair_rows.extend(directed)
        by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in directed:
            by_source[_clean(row["source_unit_code"])].append(row)
        unit_summaries: list[dict[str, Any]] = []
        for unit in raw_units:
            unit_code = _clean(unit.get("unit_code"))
            rows = by_source.get(unit_code, [])
            adjusted_values = [float(row["ontology_adjusted_transferability_ratio"]) for row in rows]
            exact_values = [float(row["exact_ksa_overlap_ratio"]) for row in rows]
            adjusted_minus_exact_values = [float(row["adjusted_minus_exact"]) for row in rows]
            baseline_dependency_values = [float(row["baseline_dependency_ratio"]) for row in rows]
            top_targets = sorted(
                rows,
                key=lambda row: (
                    -float(row["ontology_adjusted_transferability_ratio"]),
                    _clean(row.get("target_unit_name")),
                ),
            )[:5]
            unit_summaries.append(
                {
                    "unit_code": unit_code,
                    "unit_name": _unit_label(unit),
                    "unit_level": unit.get("unit_level_raw"),
                    "avg_adjusted": _average(adjusted_values),
                    "avg_exact": _average(exact_values),
                    "avg_adjusted_minus_exact": _average(adjusted_minus_exact_values),
                    "baseline_dependency_ratio": _average(baseline_dependency_values),
                    "target_count": len(rows),
                    "top_target_units": [
                        {
                            "unit_name": row["target_unit_name"],
                            "unit_code": row["target_unit_code"],
                            "adjusted": row["ontology_adjusted_transferability_ratio"],
                            "exact": row["exact_ksa_overlap_ratio"],
                            "baseline_dependency_ratio": row["baseline_dependency_ratio"],
                        }
                        for row in top_targets
                    ],
                }
            )
        unit_summaries.sort(key=lambda item: (-float(item["avg_adjusted"]), item["unit_name"]))
        has_absolute_core = any(float(item["avg_adjusted"]) >= 0.34 for item in unit_summaries)
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        matrix: list[dict[str, Any]] = []
        for index, summary in enumerate(unit_summaries, start=1):
            unit = next(unit for unit in raw_units if _clean(unit.get("unit_code")) == summary["unit_code"])
            course_link = course_links_by_unit.get(summary["unit_code"], {})
            group = _education_group(
                float(summary["avg_adjusted"]),
                avg_exact=float(summary["avg_exact"]),
                baseline_dependency_ratio=float(summary["baseline_dependency_ratio"]),
                linked_training_course_count=int(course_link.get("linked_training_course_count") or 0),
                rank=index,
                total=len(unit_summaries),
                has_absolute_core=has_absolute_core,
            )
            grouped_summary = dict(summary)
            grouped_summary["education_group"] = group
            groups[group["code"]].append(grouped_summary)
            matrix.append(
                _matrix_row(
                    scope=scope,
                    unit=unit,
                    summary=summary,
                    group=group,
                    course_link=course_link,
                )
            )
        undirected: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in directed:
            undirected[_pair_key(row["source_unit_code"], row["target_unit_code"])].append(row)
        pair_summaries: list[dict[str, Any]] = []
        for key, rows in undirected.items():
            values = [float(row["ontology_adjusted_transferability_ratio"]) for row in rows]
            exact_values = [float(row["exact_ksa_overlap_ratio"]) for row in rows]
            baseline_dependency_values = [float(row["baseline_dependency_ratio"]) for row in rows]
            names = sorted({_clean(row["source_unit_name"]) for row in rows} | {_clean(row["target_unit_name"]) for row in rows})
            pair_summaries.append(
                {
                    "unit_a": names[0] if names else key[0],
                    "unit_b": names[1] if len(names) > 1 else key[1],
                    "unit_a_code": key[0],
                    "unit_b_code": key[1],
                    "mean_adjusted": _average(values),
                    "max_adjusted": round(max(values), 4) if values else 0.0,
                    "min_adjusted": round(min(values), 4) if values else 0.0,
                    "mean_exact": _average(exact_values),
                    "mean_adjusted_minus_exact": round(max(0.0, _average(values) - _average(exact_values)), 4),
                    "mean_baseline_dependency_ratio": _average(baseline_dependency_values),
                }
            )
        pair_summaries.sort(key=lambda item: (-float(item["mean_adjusted"]), item["unit_a"], item["unit_b"]))
        adjusted_scope_values = [float(row["ontology_adjusted_transferability_ratio"]) for row in directed]
        exact_scope_values = [float(row["exact_ksa_overlap_ratio"]) for row in directed]
        adjusted_minus_exact_scope_values = [float(row["adjusted_minus_exact"]) for row in directed]
        baseline_heavy_count = sum(
            1
            for row in directed
            if float(row["ontology_adjusted_transferability_ratio"]) >= 0.32
            and float(row["exact_ksa_overlap_ratio"]) <= 0.015
        )
        report_scopes.append(
            {
                "classification": scope,
                "scope_label": _scope_label(scope),
                "unit_count": len(raw_units),
                "directed_pair_count": len(directed),
                "undirected_pair_count": len(pair_summaries),
                "score_summary": {
                    "avg_adjusted": _average(adjusted_scope_values),
                    "avg_exact": _average(exact_scope_values),
                    "avg_adjusted_minus_exact": _average(adjusted_minus_exact_scope_values),
                    "baseline_heavy_pair_ratio": round(baseline_heavy_count / len(directed), 4) if directed else 0.0,
                    "max_adjusted": round(max(adjusted_scope_values), 4) if adjusted_scope_values else 0.0,
                    "min_adjusted": round(min(adjusted_scope_values), 4) if adjusted_scope_values else 0.0,
                },
                "top_undirected_pairs": pair_summaries[:top_pairs],
                "average_by_unit": unit_summaries[:top_units],
                "education_system": {
                    "groups": {code: items for code, items in groups.items()},
                    "recommended_path": _scope_recommended_path(groups),
                    "training_system_matrix": matrix,
                    "human_review": {
                        "status": "needs_review",
                        "rationale": "Ontology-adjusted grouping is an automated draft and must not be treated as reviewed.",
                    },
                },
            }
        )
    report_scopes.sort(
        key=lambda item: (
            -float(item["score_summary"]["avg_adjusted"]),
            item["scope_label"],
        )
    )
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": _now(),
        "scope": {
            "major_code": major_code,
            "limit_scopes": limit_scopes,
            "min_units": min_units,
            "max_units_per_scope": max_units_per_scope,
        },
        "method": {
            "name": "exact_ksa_overlap_plus_ncs_scope_ontology_task_similarity_job_base_qualification",
            "same_sub_classification_baseline": 0.24,
            "role_overlay": "not_applied_in_batch_profiles",
            "interpretation_bands": [
                {"min": 0.55, "label": "strong_transfer"},
                {"min": 0.40, "label": "adjacent_bridge"},
                {"min": 0.30, "label": "same_scope_supplement"},
                {"min": 0.0, "label": "baseline_or_review"},
            ],
        },
        "summary": {
            "scope_count": len(report_scopes),
            "skipped_scope_count": len(skipped),
            "unit_count": sum(item["unit_count"] for item in report_scopes),
            "directed_pair_count": directed_count,
            "pair_csv_available": False,
            "human_review_required": True,
        },
        "skipped_scopes": skipped,
        "scopes": report_scopes,
    }


def build_ontology_transferability_field_review(major_run_path: Path) -> dict[str, Any]:
    run, loaded_reports = _load_major_transferability_reports(major_run_path)
    major_reviews: list[dict[str, Any]] = []
    totals = {
        "scope_count": 0,
        "unit_count": 0,
        "directed_pair_count": 0,
        "failed_major_count": 0,
        "missing_artifact_count": 0,
    }
    batch_seconds = 0.0
    for item in loaded_reports:
        result = item["run_result"]
        report = item["report"]
        artifact_path = item["artifact_path"]
        batch_seconds += float(result.get("seconds") or 0.0)
        if report is None:
            totals["failed_major_count"] += 1
            if item.get("load_error"):
                totals["missing_artifact_count"] += 1
            major_reviews.append(
                {
                    "major_code": result.get("major_code"),
                    "major_name": result.get("major_name"),
                    "ok": False,
                    "artifact_path": str(artifact_path),
                    "load_error": item.get("load_error"),
                    "returncode": result.get("returncode"),
                    "plausibility": "artifact_unavailable",
                }
            )
            continue
        summary = report.get("summary") or {}
        scopes = report.get("scopes") or []
        totals["scope_count"] += int(summary.get("scope_count") or 0)
        totals["unit_count"] += int(summary.get("unit_count") or 0)
        totals["directed_pair_count"] += int(summary.get("directed_pair_count") or 0)
        top_scope = scopes[0] if scopes else {}
        top_score = top_scope.get("score_summary") or {}
        baseline_heavy_scope_count = sum(
            1 for scope in scopes if _is_baseline_heavy_scope(scope.get("score_summary") or {})
        )
        no_course_link_scope_count = sum(
            1
            for scope in scopes
            if "no_direct_training_course_links_in_matrix" in _scope_review_flags(scope)
        )
        major_reviews.append(
            {
                "major_code": result.get("major_code"),
                "major_name": result.get("major_name"),
                "ok": True,
                "artifact_path": str(artifact_path),
                "scope_count": int(summary.get("scope_count") or 0),
                "unit_count": int(summary.get("unit_count") or 0),
                "directed_pair_count": int(summary.get("directed_pair_count") or 0),
                "top_scope": {
                    "scope_label": top_scope.get("scope_label"),
                    "score_summary": top_score,
                    "top_hubs": _scope_top_hubs(top_scope),
                    "review_flags": _scope_review_flags(top_scope),
                },
                "top_avg_adjusted": _score_value(top_score, "avg_adjusted"),
                "top_avg_exact": _score_value(top_score, "avg_exact"),
                "top_avg_adjusted_minus_exact": _score_value(top_score, "avg_adjusted_minus_exact"),
                "top_baseline_heavy_pair_ratio": _score_value(top_score, "baseline_heavy_pair_ratio"),
                "plausibility": _scope_plausibility(top_score),
                "baseline_heavy_scope_count": baseline_heavy_scope_count,
                "no_course_link_scope_count": no_course_link_scope_count,
            }
        )
    major_reviews.sort(key=lambda item: _clean(item.get("major_code")))
    return {
        "schema": FIELD_REVIEW_SCHEMA_VERSION,
        "generated_at": _now(),
        "source_run": str(major_run_path),
        "major_count": len(major_reviews),
        "ok": totals["failed_major_count"] == 0 and totals["scope_count"] > 0,
        "totals": {
            **totals,
            "batch_seconds": round(batch_seconds, 3),
        },
        "major_reviews": major_reviews,
        "method_adjustment_candidates": [
            {
                "priority": "done_for_batch_v2",
                "topic": "baseline_dependency_metric",
                "recommendation": (
                    "Use avg_adjusted_minus_exact and baseline_heavy_pair_ratio as review filters "
                    "before changing score thresholds."
                ),
            },
            {
                "priority": "high",
                "topic": "field_by_field_review_loop",
                "recommendation": (
                    "For each major, review one high-support scope, one typical middle scope, "
                    "and one baseline-heavy or low-evidence scope."
                ),
            },
            {
                "priority": "medium",
                "topic": "external_curriculum_spot_check",
                "recommendation": (
                    "Compare sampled generated education systems with public curricula, official "
                    "NCS unit materials, or training-course evidence before accepting grouping thresholds."
                ),
            },
            {
                "priority": "medium",
                "topic": "role_overlay_by_domain",
                "recommendation": (
                    "Keep role overlays outside the all-domain batch score until a domain-specific "
                    "review confirms the overlay logic."
                ),
            },
        ],
    }


def _select_seed_scopes(scopes: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    if not scopes:
        return []
    ranked = sorted(
        scopes,
        key=lambda scope: (
            -_score_value(scope.get("score_summary") or {}, "avg_adjusted"),
            _clean(scope.get("scope_label")),
        ),
    )
    selections: list[tuple[str, dict[str, Any]]] = []
    selected_scope_keys: set[str] = set()

    def scope_key(scope: dict[str, Any]) -> str:
        classification = scope.get("classification") or {}
        return _clean(classification.get("classification_id")) or _clean(scope.get("scope_label"))

    def add_selection(purpose: str, candidates: list[dict[str, Any]]) -> None:
        for candidate in candidates:
            key = scope_key(candidate)
            if key not in selected_scope_keys:
                selections.append((purpose, candidate))
                selected_scope_keys.add(key)
                return
        if candidates:
            selections.append((purpose, candidates[0]))

    add_selection("high_support", ranked)
    middle_index = min(len(ranked) - 1, max(0, len(ranked) // 2))
    middle_candidates = ranked[middle_index:] + ranked[:middle_index]
    add_selection("typical_middle", middle_candidates)
    baseline_candidates = [
        scope
        for scope in ranked
        if _is_baseline_heavy_scope(scope.get("score_summary") or {})
        or _score_value(scope.get("score_summary") or {}, "avg_adjusted") < 0.3
    ]
    if baseline_candidates:
        baseline_candidates.sort(
            key=lambda scope: (
                -_score_value(scope.get("score_summary") or {}, "baseline_heavy_pair_ratio"),
                _score_value(scope.get("score_summary") or {}, "avg_exact"),
                -_score_value(scope.get("score_summary") or {}, "avg_adjusted"),
            )
        )
        add_selection("baseline_or_low_evidence", baseline_candidates)
    elif len(ranked) > 1:
        add_selection("baseline_or_low_evidence", list(reversed(ranked)))
    return selections


def build_ontology_transferability_review_seedpack(major_run_path: Path) -> dict[str, Any]:
    _, loaded_reports = _load_major_transferability_reports(major_run_path)
    source_run_fingerprint = _file_fingerprint(major_run_path)
    seeds: list[dict[str, Any]] = []
    load_errors: list[dict[str, Any]] = []
    for item in loaded_reports:
        result = item["run_result"]
        report = item["report"]
        if report is None:
            load_errors.append(
                {
                    "major_code": result.get("major_code"),
                    "major_name": result.get("major_name"),
                    "returncode": result.get("returncode"),
                    "json_path": result.get("json_path"),
                    "load_error": item.get("load_error"),
                }
            )
            continue
        artifact_path = item["artifact_path"]
        for purpose, scope in _select_seed_scopes(report.get("scopes") or []):
            seeds.append(
                {
                    "schema": REVIEW_SEED_SCHEMA_VERSION,
                    "major_code": result.get("major_code"),
                    "major_name": result.get("major_name"),
                    "review_purpose": purpose,
                    "scope_label": scope.get("scope_label"),
                    "classification": scope.get("classification"),
                    "score_summary": scope.get("score_summary"),
                    "top_hub_units": _scope_top_hubs(scope, limit=8),
                    "top_undirected_pairs": (scope.get("top_undirected_pairs") or [])[:5],
                    "matrix_sample": _matrix_sample(scope, limit=8),
                    "review_flags": _scope_review_flags(scope),
                    "review_prompt": (
                        "Compare the generated required/optional unit grouping with public curriculum, "
                        "NCS training-course evidence, and domain expert judgment. Do not mark reviewed automatically."
                    ),
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "rationale": "",
                    "source_artifact": str(artifact_path),
                }
            )
    exported_at = _now()
    seedpack_id = _seedpack_id(exported_at, major_run_path, len(seeds))
    for sequence, seed in enumerate(seeds, start=1):
        seed["record_type"] = "ontology_transferability_review_item"
        seed["format_version"] = REVIEW_SEED_SCHEMA_VERSION
        seed["seedpack_id"] = seedpack_id
        seed["sequence"] = sequence
        seed["target_snapshot_hash"] = _hash_payload(
            {
                "major_code": seed.get("major_code"),
                "review_purpose": seed.get("review_purpose"),
                "scope_label": seed.get("scope_label"),
                "score_summary": seed.get("score_summary"),
                "matrix_sample": seed.get("matrix_sample"),
            }
        )
    ok = len(load_errors) == 0 and bool(seeds)
    return {
        "schema": REVIEW_SEEDPACK_SCHEMA_VERSION,
        "record_type": "batch",
        "format_version": REVIEW_SEEDPACK_SCHEMA_VERSION,
        "seedpack_id": seedpack_id,
        "generated_at": exported_at,
        "source_run": str(major_run_path),
        "source_run_resolved": source_run_fingerprint.get("resolved_path"),
        "source_run_fingerprint": source_run_fingerprint,
        "encoding": "utf-8",
        "allowed_decisions": ALLOWED_REVIEW_DECISIONS,
        "ok": ok,
        "seed_count": len(seeds),
        "failed_major_count": len(load_errors),
        "load_errors": load_errors,
        "seeds": seeds,
        "human_decision_fields_blank": True,
        "notes": [
            "This seedpack is export-only and must not be applied as human review without explicit decisions.",
            "Decision fields are intentionally blank.",
        ],
    }


def build_ontology_transferability_calibration(major_run_path: Path) -> dict[str, Any]:
    _, loaded_reports = _load_major_transferability_reports(major_run_path)
    adjusted_bands = [
        ("strong_transfer_ge_0.55", 0.55, None),
        ("adjacent_bridge_0.40_0.55", 0.40, 0.55),
        ("same_scope_supplement_0.30_0.40", 0.30, 0.40),
        ("baseline_or_low_lt_0.30", None, 0.30),
    ]
    exact_bands = [
        ("exact_ge_0.15", 0.15, None),
        ("exact_0.05_0.15", 0.05, 0.15),
        ("exact_0.01_0.05", 0.01, 0.05),
        ("exact_lt_0.01", None, 0.01),
    ]
    baseline_thresholds = [0.05, 0.10, 0.20, 0.30]
    low_exact_floors = [0.01, 0.03, 0.05, 0.10]

    totals = {
        "major_count": 0,
        "scope_count": 0,
        "matrix_row_count": 0,
        "matrix_rows_with_course_links": 0,
        "strong_support_scope_count": 0,
        "sample_review_scope_count": 0,
        "manual_priority_scope_count": 0,
        "no_course_link_scope_count": 0,
    }
    band_counts = {
        "adjusted": {label: 0 for label, _, _ in adjusted_bands},
        "exact": {label: 0 for label, _, _ in exact_bands},
    }
    sensitivity = {
        "baseline_heavy_pair_ratio_thresholds": {str(threshold): 0 for threshold in baseline_thresholds},
        "low_exact_high_adjusted_floors": {str(floor): 0 for floor in low_exact_floors},
    }
    major_rows: list[dict[str, Any]] = []
    load_errors: list[dict[str, Any]] = []
    for item in loaded_reports:
        result = item["run_result"]
        report = item["report"]
        if report is None:
            load_errors.append(
                {
                    "major_code": result.get("major_code"),
                    "major_name": result.get("major_name"),
                    "returncode": result.get("returncode"),
                    "json_path": result.get("json_path"),
                    "load_error": item.get("load_error"),
                }
            )
            major_rows.append(
                {
                    "major_code": result.get("major_code"),
                    "major_name": result.get("major_name"),
                    "ok": False,
                    "load_error": item.get("load_error"),
                }
            )
            continue
        totals["major_count"] += 1
        scopes = report.get("scopes") or []
        major_counts = {
            "scope_count": 0,
            "strong_support_scope_count": 0,
            "sample_review_scope_count": 0,
            "manual_priority_scope_count": 0,
            "no_course_link_scope_count": 0,
            "matrix_row_count": 0,
            "matrix_rows_with_course_links": 0,
        }
        for scope in scopes:
            score = scope.get("score_summary") or {}
            avg_adjusted = _score_value(score, "avg_adjusted")
            avg_exact = _score_value(score, "avg_exact")
            baseline_ratio = _score_value(score, "baseline_heavy_pair_ratio")
            has_course_link = _scope_has_any_course_link(scope)
            flags = _scope_review_flags(scope)
            matrix = (scope.get("education_system") or {}).get("training_system_matrix") or []
            matrix_with_course = sum(
                1
                for row in matrix
                if int(((row.get("course_link") or {}).get("linked_training_course_count") or 0)) > 0
            )
            totals["scope_count"] += 1
            totals["matrix_row_count"] += len(matrix)
            totals["matrix_rows_with_course_links"] += matrix_with_course
            major_counts["scope_count"] += 1
            major_counts["matrix_row_count"] += len(matrix)
            major_counts["matrix_rows_with_course_links"] += matrix_with_course
            adjusted_band = _score_band(avg_adjusted, adjusted_bands)
            exact_band = _score_band(avg_exact, exact_bands)
            band_counts["adjusted"][adjusted_band] += 1
            band_counts["exact"][exact_band] += 1
            for threshold in baseline_thresholds:
                if baseline_ratio >= threshold:
                    sensitivity["baseline_heavy_pair_ratio_thresholds"][str(threshold)] += 1
            for floor in low_exact_floors:
                if avg_adjusted >= 0.34 and avg_exact <= floor:
                    sensitivity["low_exact_high_adjusted_floors"][str(floor)] += 1
            if not has_course_link:
                totals["no_course_link_scope_count"] += 1
                major_counts["no_course_link_scope_count"] += 1
            if (
                avg_adjusted >= 0.5
                and avg_exact >= 0.15
                and baseline_ratio <= 0.1
                and has_course_link
            ):
                policy_bucket = "strong_support_scope_count"
            elif flags or not has_course_link:
                policy_bucket = "manual_priority_scope_count"
            else:
                policy_bucket = "sample_review_scope_count"
            totals[policy_bucket] += 1
            major_counts[policy_bucket] += 1
        major_rows.append(
            {
                "major_code": result.get("major_code"),
                "major_name": result.get("major_name"),
                "ok": True,
                **major_counts,
                "course_link_row_coverage": round(
                    major_counts["matrix_rows_with_course_links"] / major_counts["matrix_row_count"],
                    4,
                )
                if major_counts["matrix_row_count"]
                else 0.0,
            }
        )
    course_link_row_coverage = (
        round(totals["matrix_rows_with_course_links"] / totals["matrix_row_count"], 4)
        if totals["matrix_row_count"]
        else 0.0
    )
    return {
        "schema": CALIBRATION_SCHEMA_VERSION,
        "generated_at": _now(),
        "source_run": str(major_run_path),
        "ok": len(load_errors) == 0 and totals["scope_count"] > 0,
        "failed_major_count": len(load_errors),
        "load_errors": load_errors,
        "totals": {
            **totals,
            "course_link_row_coverage": course_link_row_coverage,
        },
        "band_counts": band_counts,
        "sensitivity": sensitivity,
        "major_rows": sorted(major_rows, key=lambda item: _clean(item.get("major_code"))),
        "provisional_policy": [
            {
                "bucket": "strong_support",
                "rule": "avg_adjusted >= 0.50 and avg_exact >= 0.15 and baseline_heavy_pair_ratio <= 0.10 and at least one course link exists",
                "use": "candidate for lighter sampling, still not human-reviewed automatically",
            },
            {
                "bucket": "sample_review",
                "rule": "not strong_support and no review flags",
                "use": "review one typical-middle scope per major before threshold changes",
            },
            {
                "bucket": "manual_priority",
                "rule": "baseline-heavy, low-exact/high-adjusted, or no direct training-course links",
                "use": "requires human review or external curriculum evidence before using as education-system draft",
            },
        ],
    }


def build_ontology_transferability_method_work_queue(
    calibration_path: Path,
    seedpack_path: Path,
    *,
    field_review_path: Path | None = None,
    spotcheck_plan_path: Path | None = None,
    external_spotcheck_path: Path | None = None,
    course_link_gap_diagnostic_path: Path | None = None,
) -> dict[str, Any]:
    calibration = _load_json(calibration_path)
    if calibration.get("schema") != CALIBRATION_SCHEMA_VERSION:
        raise ValueError(
            f"Invalid ontology transferability calibration schema: {calibration.get('schema')!r}. "
            f"Expected {CALIBRATION_SCHEMA_VERSION!r}."
        )
    batch, _ = _load_seedpack_records(seedpack_path)
    totals = calibration.get("totals") or {}
    major_rows = [
        row
        for row in calibration.get("major_rows") or []
        if isinstance(row, dict) and row.get("ok") is not False
    ]
    source_artifacts = {
        "calibration": str(calibration_path),
        "review_seedpack": str(seedpack_path),
    }
    validation_issues: list[dict[str, Any]] = []
    course_gap_diagnostic_summary: dict[str, Any] = {
        "scope_count": 0,
        "issue_type_counts": {},
        "major_issue_counts": [],
    }

    def add_validation_issue(code: str, message: str, **extra: Any) -> None:
        validation_issues.append({"code": code, "message": message, **extra})

    if field_review_path:
        source_artifacts["field_review"] = str(field_review_path)
        if not field_review_path.exists():
            add_validation_issue(
                "field_review_missing",
                "Supplied field-review artifact does not exist.",
                path=str(field_review_path),
            )
        else:
            try:
                field_review = _load_json(field_review_path)
                if field_review.get("schema") != FIELD_REVIEW_SCHEMA_VERSION:
                    add_validation_issue(
                        "field_review_schema_mismatch",
                        "Supplied field-review artifact has an invalid schema.",
                        schema=field_review.get("schema"),
                    )
                if not _same_reference(
                    field_review.get("source_run"),
                    calibration.get("source_run"),
                    left_base=field_review_path,
                    right_base=calibration_path,
                ):
                    add_validation_issue(
                        "field_review_source_run_mismatch",
                        "Field-review source run does not match calibration source run.",
                        expected=calibration.get("source_run"),
                        actual=field_review.get("source_run"),
                    )
            except (OSError, json.JSONDecodeError) as exc:
                add_validation_issue(
                    "field_review_unreadable",
                    "Supplied field-review artifact could not be read.",
                    path=str(field_review_path),
                    error=str(exc),
                )
    if spotcheck_plan_path:
        source_artifacts["spotcheck_plan"] = str(spotcheck_plan_path)
        if not spotcheck_plan_path.exists():
            add_validation_issue(
                "spotcheck_plan_missing",
                "Supplied spotcheck-plan artifact does not exist.",
                path=str(spotcheck_plan_path),
            )
        else:
            try:
                spotcheck = _load_json(spotcheck_plan_path)
                if spotcheck.get("schema") != SPOTCHECK_PLAN_SCHEMA_VERSION:
                    add_validation_issue(
                        "spotcheck_plan_schema_mismatch",
                        "Supplied spotcheck-plan artifact has an invalid schema.",
                        schema=spotcheck.get("schema"),
                    )
                if spotcheck.get("seedpack_id") != batch.get("seedpack_id"):
                    add_validation_issue(
                        "spotcheck_plan_seedpack_mismatch",
                        "Spotcheck plan seedpack id does not match review seedpack.",
                        expected=batch.get("seedpack_id"),
                        actual=spotcheck.get("seedpack_id"),
                    )
            except (OSError, json.JSONDecodeError) as exc:
                add_validation_issue(
                    "spotcheck_plan_unreadable",
                    "Supplied spotcheck-plan artifact could not be read.",
                    path=str(spotcheck_plan_path),
                    error=str(exc),
                )
    if external_spotcheck_path:
        source_artifacts["external_spot_check"] = str(external_spotcheck_path)
        if not external_spotcheck_path.exists() or not external_spotcheck_path.is_file():
            add_validation_issue(
                "external_spotcheck_missing",
                "Supplied external spot-check artifact does not exist.",
                path=str(external_spotcheck_path),
            )
        elif external_spotcheck_path.stat().st_size <= 0:
            add_validation_issue(
                "external_spotcheck_empty",
                "Supplied external spot-check artifact is empty.",
                path=str(external_spotcheck_path),
            )

    if course_link_gap_diagnostic_path:
        source_artifacts["course_link_gap_diagnostic"] = str(course_link_gap_diagnostic_path)
        if not course_link_gap_diagnostic_path.exists():
            add_validation_issue(
                "course_link_gap_diagnostic_missing",
                "Supplied course-link gap diagnostic artifact does not exist.",
                path=str(course_link_gap_diagnostic_path),
            )
        else:
            try:
                diagnostic = _load_json(course_link_gap_diagnostic_path)
                if diagnostic.get("schema") != COURSE_LINK_GAP_DIAGNOSTIC_SCHEMA_VERSION:
                    add_validation_issue(
                        "course_link_gap_diagnostic_schema_mismatch",
                        "Supplied course-link gap diagnostic artifact has an invalid schema.",
                        schema=diagnostic.get("schema"),
                    )
                if not _same_reference(
                    diagnostic.get("source_run"),
                    calibration.get("source_run"),
                    left_base=course_link_gap_diagnostic_path,
                    right_base=calibration_path,
                ):
                    add_validation_issue(
                        "course_link_gap_diagnostic_source_run_mismatch",
                        "Course-link gap diagnostic source run does not match calibration source run.",
                        expected=calibration.get("source_run"),
                        actual=diagnostic.get("source_run"),
                    )
                if diagnostic.get("schema") == COURSE_LINK_GAP_DIAGNOSTIC_SCHEMA_VERSION:
                    course_gap_diagnostic_summary = {
                        "scope_count": int(diagnostic.get("scope_count") or 0),
                        "issue_type_counts": diagnostic.get("issue_type_counts") or {},
                        "major_issue_counts": [
                            row
                            for row in diagnostic.get("major_issue_counts") or []
                            if isinstance(row, dict)
                        ],
                    }
            except (OSError, json.JSONDecodeError) as exc:
                add_validation_issue(
                    "course_link_gap_diagnostic_unreadable",
                    "Supplied course-link gap diagnostic artifact could not be read.",
                    path=str(course_link_gap_diagnostic_path),
                    error=str(exc),
                )

    queue_items: list[dict[str, Any]] = []
    field_rows = sorted(
        [row for row in major_rows if int(row.get("manual_priority_scope_count") or 0) > 0],
        key=lambda row: (
            -int(row.get("manual_priority_scope_count") or 0),
            -int(row.get("no_course_link_scope_count") or 0),
            float(row.get("course_link_row_coverage") or 0.0),
            _clean(row.get("major_code")),
        ),
    )
    for index, row in enumerate(field_rows[:12], start=1):
        priority = "P0" if index <= 4 else "P1"
        queue_items.append(
            {
                "priority": priority,
                "track": "field_review",
                "major_code": row.get("major_code"),
                "major_name": row.get("major_name"),
                "reason": (
                    f"manual_priority_scope_count={int(row.get('manual_priority_scope_count') or 0)}, "
                    f"no_course_link_scope_count={int(row.get('no_course_link_scope_count') or 0)}, "
                    f"course_link_row_coverage={float(row.get('course_link_row_coverage') or 0.0)}"
                ),
                "recommended_action": (
                    "Review high/middle/baseline seedpack rows against NCS unit evidence and at least "
                    "one public curriculum/job-description source."
                ),
                "source_artifacts": [str(calibration_path), str(seedpack_path)],
            }
        )

    gap_rows = sorted(
        [row for row in major_rows if int(row.get("no_course_link_scope_count") or 0) > 0],
        key=lambda row: (
            -int(row.get("no_course_link_scope_count") or 0),
            float(row.get("course_link_row_coverage") or 0.0),
            _clean(row.get("major_code")),
        ),
    )
    for row in gap_rows:
        queue_items.append(
            {
                "priority": "P1",
                "track": "training_course_link_gap",
                "major_code": row.get("major_code"),
                "major_name": row.get("major_name"),
                "reason": (
                    f"no_course_link_scope_count={int(row.get('no_course_link_scope_count') or 0)}, "
                    f"matrix_row_coverage={float(row.get('course_link_row_coverage') or 0.0)}"
                ),
                "recommended_action": (
                    "Inspect whether unit-code training-course links are genuinely missing or whether "
                    "collection/linking needs repair before score tuning."
                ),
                "source_artifacts": [str(calibration_path)],
            }
        )

    diagnostic_gap_rows = sorted(
        [
            row
            for row in course_gap_diagnostic_summary.get("major_issue_counts") or []
            if int(row.get("scope_count") or 0) > 0
        ],
        key=lambda row: (
            -int(row.get("scope_count") or 0),
            _clean(row.get("issue_type")),
            _clean(row.get("major_code")),
        ),
    )
    for row in diagnostic_gap_rows:
        issue_type = _clean(row.get("issue_type"))
        diagnostic_priority = (
            "P2"
            if issue_type in {"likely_no_training_course_rows", "cross_scope_name_only"}
            else "P1"
        )
        diagnostic_action = (
            "Treat same-name or similar-name matches outside the target NCS major as adjacent "
            "references only; verify public curriculum evidence before creating any link."
            if issue_type == "cross_scope_name_only"
            else (
                "Review row-level course-link gaps from the diagnostic before treating the "
                "education-system matrix as course-deliverable."
            )
        )
        queue_items.append(
            {
                "priority": diagnostic_priority,
                "track": "training_course_link_gap_diagnostic",
                "major_code": row.get("major_code"),
                "major_name": row.get("major_name"),
                "reason": (
                    f"diagnostic_scope_count={int(row.get('scope_count') or 0)}, "
                    f"issue_type={issue_type}"
                ),
                "recommended_action": diagnostic_action,
                "source_artifacts": [str(course_link_gap_diagnostic_path)] if course_link_gap_diagnostic_path else [],
            }
        )

    sensitivity = calibration.get("sensitivity") or {}
    baseline_heavy = (sensitivity.get("baseline_heavy_pair_ratio_thresholds") or {}).get("0.2")
    low_exact_high_adjusted = (sensitivity.get("low_exact_high_adjusted_floors") or {}).get("0.03")
    queue_items.append(
        {
            "priority": "P0",
            "track": "score_policy",
            "major_code": "ALL",
            "major_name": "All NCS majors",
            "reason": (
                f"baseline_heavy_pair_ratio >= 0.2 flags {baseline_heavy} scopes; "
                f"avg_adjusted >= 0.34 with avg_exact <= 0.03 flags {low_exact_high_adjusted} scopes."
            ),
            "recommended_action": (
                "Do not raise or lower the same-sub-classification baseline until P0 field samples are reviewed."
            ),
            "source_artifacts": [str(calibration_path)],
        }
    )

    hr_row = next((row for row in major_rows if _clean(row.get("major_code")) == "02"), None)
    if hr_row:
        queue_items.append(
            {
                "priority": "P0",
                "track": "role_overlay",
                "major_code": "02",
                "major_name": hr_row.get("major_name") or "Business/Accounting/Office",
                "reason": (
                    "HR team-lead and manager targets need role-responsibility overlay beyond "
                    "NCS unit-average transferability."
                ),
                "recommended_action": (
                    "Keep manager/team-lead role overlay as a separate route-layer signal, not a global "
                    "ontology-adjusted batch score."
                ),
                "source_artifacts": [str(calibration_path), str(seedpack_path)],
            }
        )

    return {
        "schema": METHOD_WORK_QUEUE_SCHEMA_VERSION,
        "generated_at": _now(),
        "ok": bool(calibration.get("ok"))
        and not batch.get("failed_major_count")
        and not validation_issues,
        "source_artifacts": source_artifacts,
        "source_artifact_fingerprints": {
            key: _file_fingerprint(Path(value))
            for key, value in source_artifacts.items()
            if value
        },
        "validation_issues": validation_issues,
        "seedpack_contract": {
            "seedpack_id": batch.get("seedpack_id"),
            "format_version": batch.get("schema"),
            "seed_count": batch.get("seed_count"),
            "failed_major_count": batch.get("failed_major_count"),
            "allowed_decisions": batch.get("allowed_decisions") or [],
        },
        "totals": totals,
        "course_link_gap_diagnostic_summary": course_gap_diagnostic_summary,
        "queue_count": len(queue_items),
        "queue_items": queue_items,
        "acceptance_rules": [
            "Do not tune global thresholds from avg_adjusted alone; compare avg_exact, avg_adjusted_minus_exact, baseline_heavy_pair_ratio, course links, hours, methods, and facilities.",
            "All automatic review seed rows must keep decision/reviewer/reviewed_at/rationale blank until a human decision is supplied.",
            "Threshold changes require at least one high-support, one typical-middle, and one manual-priority sample per major.",
            "A high-support sample should be externally plausible; a typical-middle sample should be explainable; weak evidence must not be promoted to required, and no-course-link rows must remain review-gated.",
            "External spot-check evidence can justify method adjustment but must not be inserted as scored source training data.",
            "Role overlay is allowed only in query/planner interpretation layers until reviewed domain-by-domain.",
        ],
    }


def build_ontology_transferability_artifact_audit(
    major_run_path: Path,
    *,
    seedpack_path: Path | None = None,
    spotcheck_plan_path: Path | None = None,
    method_work_queue_path: Path | None = None,
) -> dict[str, Any]:
    run, loaded_reports = _load_major_transferability_reports(major_run_path)
    issues: list[dict[str, Any]] = []
    counts = {
        "major_result_count": len(run.get("results") or []),
        "loaded_major_count": 0,
        "scope_count": 0,
        "matrix_row_count": 0,
        "recommended_path_count": 0,
        "seed_count": 0,
        "spotcheck_count": 0,
        "method_queue_count": 0,
        "method_queue_p0_count": 0,
    }
    loaded_major_codes: set[str] = set()
    loaded_artifact_paths: set[Path] = set()
    required_row_fields = {
        "job_scope",
        "unit_code",
        "unit_name",
        "target_level_band",
        "education_type",
        "required_optional_basis",
        "delivery_operation",
        "planner_grouping",
        "task_ksa_basis",
        "facility_constraint_fit",
        "human_review",
        "course_link",
        "course_fit",
    }
    required_task_ksa_fields = {
        "average_adjusted_transferability",
        "average_exact_ksa_overlap",
        "average_adjusted_minus_exact",
        "baseline_dependency_ratio",
        "top_target_units",
        "basis_types",
    }
    required_facility_fit_fields = {"status", "requested", "available", "matched", "missing", "rationale"}
    required_course_fit_fields = {"level", "hours", "methods", "facilities"}
    expected_path_roles = {
        "scope_confirmation": "C1-1",
        "core_gap_training": "C1-2",
        "supporting_or_adjacent_training": "C2-1",
        "delivery_fit_review": "C2-2",
    }
    forbidden_review_statuses = {"human_reviewed", "accepted", "reviewed"}

    def add_issue(code: str, message: str, **extra: Any) -> None:
        issues.append({"code": code, "message": message, **extra})

    for item in loaded_reports:
        result = item["run_result"]
        report = item["report"]
        major_code = _clean(result.get("major_code"))
        if report is None:
            add_issue(
                "major_artifact_not_loaded",
                "Major artifact could not be loaded.",
                major_code=major_code,
                json_path=result.get("json_path"),
                load_error=item.get("load_error"),
            )
            continue
        counts["loaded_major_count"] += 1
        loaded_major_codes.add(major_code)
        loaded_artifact_paths.add(item["artifact_path"].resolve(strict=False))
        if report.get("schema") != SCHEMA_VERSION:
            add_issue(
                "major_artifact_schema_mismatch",
                "Major artifact schema does not match ontology-adjusted education-system schema.",
                major_code=major_code,
                schema=report.get("schema"),
            )
        for scope_index, scope in enumerate(report.get("scopes") or [], start=1):
            counts["scope_count"] += 1
            scope_label = _clean(scope.get("scope_label"))
            path = (scope.get("education_system") or {}).get("recommended_path") or []
            counts["recommended_path_count"] += len(path)
            path_by_role = {
                _clean(stage.get("role")): stage
                for stage in path
                if isinstance(stage, dict)
            }
            for role, guide_stage in expected_path_roles.items():
                stage = path_by_role.get(role)
                if not stage:
                    add_issue(
                        "recommended_path_missing_role",
                        "Recommended path is missing a required role.",
                        major_code=major_code,
                        scope_label=scope_label,
                        role=role,
                    )
                    continue
                if stage.get("guide_stage") != guide_stage:
                    add_issue(
                        "recommended_path_guide_stage_mismatch",
                        "Recommended path role has an unexpected guide stage.",
                        major_code=major_code,
                        scope_label=scope_label,
                        role=role,
                        expected=guide_stage,
                        actual=stage.get("guide_stage"),
                    )
                for field in ("guide_stage_status", "guide_stage_evidence", "units"):
                    if field not in stage:
                        add_issue(
                            "recommended_path_stage_field_missing",
                            "Recommended path stage is missing a required field.",
                            major_code=major_code,
                            scope_label=scope_label,
                            role=role,
                            field=field,
                        )
            matrix = (scope.get("education_system") or {}).get("training_system_matrix") or []
            if not matrix:
                add_issue(
                    "training_system_matrix_empty",
                    "Scope has no training-system matrix rows.",
                    major_code=major_code,
                    scope_label=scope_label,
                    scope_index=scope_index,
                )
            matrix_by_code: dict[str, dict[str, Any]] = {}
            matrix_by_name: dict[str, dict[str, Any]] = {}
            for row in matrix:
                if not isinstance(row, dict):
                    continue
                unit_code = _clean(row.get("unit_code"))
                unit_name = _clean(row.get("unit_name"))
                if unit_code:
                    matrix_by_code[unit_code] = row
                if unit_name:
                    matrix_by_name[unit_name] = row

            def stage_rows(role: str) -> list[dict[str, Any]]:
                stage = path_by_role.get(role) or {}
                rows: list[dict[str, Any]] = []
                unresolved: list[str] = []
                unit_codes = [_clean(value) for value in stage.get("unit_codes") or [] if _clean(value)]
                if unit_codes:
                    for unit_code in unit_codes:
                        row = matrix_by_code.get(unit_code)
                        if row is None:
                            unresolved.append(unit_code)
                        else:
                            rows.append(row)
                else:
                    for unit_name in [_clean(value) for value in stage.get("units") or [] if _clean(value)]:
                        row = matrix_by_name.get(unit_name)
                        if row is None:
                            unresolved.append(unit_name)
                        else:
                            rows.append(row)
                for value in unresolved:
                    add_issue(
                        "recommended_path_unit_not_in_matrix",
                        "Recommended path references a unit not present in the matrix.",
                        major_code=major_code,
                        scope_label=scope_label,
                        role=role,
                        unit=value,
                    )
                return rows

            def assert_stage_required_optional(role: str, allowed: set[str]) -> None:
                for row in stage_rows(role):
                    required = _clean((row.get("required_optional_basis") or {}).get("code"))
                    if required not in allowed:
                        add_issue(
                            "recommended_path_required_optional_mismatch",
                            "Recommended path stage contains a matrix row with an incompatible required/optional classification.",
                            major_code=major_code,
                            scope_label=scope_label,
                            role=role,
                            unit_code=row.get("unit_code"),
                            unit_name=row.get("unit_name"),
                            expected=sorted(allowed),
                            actual=required,
                        )

            assert_stage_required_optional("core_gap_training", {"required"})
            assert_stage_required_optional("supporting_or_adjacent_training", {"recommended", "optional"})
            assert_stage_required_optional("delivery_fit_review", {"review"})

            review_stage_codes = {
                _clean(row.get("unit_code"))
                for row in stage_rows("delivery_fit_review")
                if _clean(row.get("unit_code"))
            }
            for row in matrix:
                if not isinstance(row, dict):
                    continue
                required = _clean((row.get("required_optional_basis") or {}).get("code"))
                unit_code = _clean(row.get("unit_code"))
                if required == "review" and unit_code and unit_code not in review_stage_codes:
                    add_issue(
                        "review_row_missing_from_delivery_review_stage",
                        "Review-gated matrix row is missing from the delivery-fit review stage.",
                        major_code=major_code,
                        scope_label=scope_label,
                        unit_code=unit_code,
                        unit_name=row.get("unit_name"),
                    )
            for row_index, row in enumerate(matrix, start=1):
                counts["matrix_row_count"] += 1
                if not isinstance(row, dict):
                    add_issue(
                        "matrix_row_not_object",
                        "Training-system matrix row is not an object.",
                        major_code=major_code,
                        scope_label=scope_label,
                        row_index=row_index,
                    )
                    continue
                missing = sorted(required_row_fields - set(row))
                if missing:
                    add_issue(
                        "matrix_row_missing_fields",
                        "Training-system matrix row is missing required fields.",
                        major_code=major_code,
                        scope_label=scope_label,
                        unit_code=row.get("unit_code"),
                        missing_fields=missing,
                    )
                task_ksa_basis = row.get("task_ksa_basis") or {}
                missing_task = sorted(required_task_ksa_fields - set(task_ksa_basis))
                if missing_task:
                    add_issue(
                        "matrix_row_task_ksa_fields_missing",
                        "Task/KSA basis is missing required audit fields.",
                        major_code=major_code,
                        scope_label=scope_label,
                        unit_code=row.get("unit_code"),
                        missing_fields=missing_task,
                    )
                facility_fit = row.get("facility_constraint_fit") or {}
                missing_facility = sorted(required_facility_fit_fields - set(facility_fit))
                if missing_facility:
                    add_issue(
                        "matrix_row_facility_fit_fields_missing",
                        "Facility constraint fit is missing required fields.",
                        major_code=major_code,
                        scope_label=scope_label,
                        unit_code=row.get("unit_code"),
                        missing_fields=missing_facility,
                    )
                course_fit = row.get("course_fit") or {}
                missing_course_fit = sorted(required_course_fit_fields - set(course_fit))
                if missing_course_fit:
                    add_issue(
                        "matrix_row_course_fit_fields_missing",
                        "Course fit is missing required fields.",
                        major_code=major_code,
                        scope_label=scope_label,
                        unit_code=row.get("unit_code"),
                        missing_fields=missing_course_fit,
                    )
                human_review = row.get("human_review") or {}
                status = _clean(human_review.get("status"))
                required = (row.get("required_optional_basis") or {}).get("code")
                task_ksa_basis = row.get("task_ksa_basis") or {}
                course_link = row.get("course_link") or {}
                if required == "required":
                    weak_flags = _evidence_review_flags(
                        avg_exact=float(task_ksa_basis.get("average_exact_ksa_overlap") or 0.0),
                        baseline_dependency_ratio=float(
                            task_ksa_basis.get("baseline_dependency_ratio") or 0.0
                        ),
                        linked_training_course_count=int(
                            course_link.get("linked_training_course_count") or 0
                        ),
                    )
                    if weak_flags:
                        add_issue(
                            "required_row_has_weak_evidence",
                            "Required row must not be emitted when exact/course/baseline evidence is weak.",
                            major_code=major_code,
                            scope_label=scope_label,
                            unit_code=row.get("unit_code"),
                            weak_flags=weak_flags,
                        )
                if status != "needs_review":
                    add_issue(
                        "matrix_row_review_status_not_needs_review",
                        "Automated matrix row must remain needs_review.",
                        major_code=major_code,
                        scope_label=scope_label,
                        unit_code=row.get("unit_code"),
                        status=status,
                    )
                if status in forbidden_review_statuses:
                    add_issue(
                        "forbidden_human_review_status",
                        "Automated artifact contains a forbidden human-review decision status.",
                        major_code=major_code,
                        scope_label=scope_label,
                        unit_code=row.get("unit_code"),
                        status=status,
                    )

    if seedpack_path:
        batch, seeds = _load_seedpack_records(seedpack_path)
        counts["seed_count"] = len(seeds)
        if batch.get("seed_count") != len(seeds):
            add_issue(
                "seedpack_count_mismatch",
                "Seedpack batch count does not match item count.",
                expected=batch.get("seed_count"),
                actual=len(seeds),
            )
        if batch.get("source_run") and not _same_reference(
            batch.get("source_run"),
            str(major_run_path),
            left_base=seedpack_path,
            right_base=major_run_path,
        ):
            add_issue(
                "seedpack_source_run_mismatch",
                "Seedpack source run does not match audited major run.",
                expected=str(major_run_path),
                actual=batch.get("source_run"),
            )
        fingerprint = batch.get("source_run_fingerprint") or {}
        current_fingerprint = _file_fingerprint(major_run_path)
        if not fingerprint:
            add_issue(
                "seedpack_source_run_fingerprint_missing",
                "Seedpack is missing source-run fingerprint metadata.",
                expected=current_fingerprint,
            )
        elif fingerprint.get("sha256") != current_fingerprint.get("sha256"):
            add_issue(
                "seedpack_source_run_fingerprint_mismatch",
                "Seedpack source-run fingerprint does not match audited major run.",
                expected=current_fingerprint,
                actual=fingerprint,
            )
        for index, seed in enumerate(seeds, start=1):
            for field in ("decision", "reviewer_id", "reviewed_at", "rationale"):
                if _clean(seed.get(field)):
                    add_issue(
                        "seedpack_decision_field_not_blank",
                        "Seedpack decision/reviewer fields must remain blank.",
                        sequence=seed.get("sequence", index),
                        field=field,
                    )
            for field in ("target_snapshot_hash", "source_artifact", "review_purpose", "matrix_sample"):
                if field not in seed:
                    add_issue(
                        "seedpack_item_field_missing",
                        "Seedpack item is missing required audit field.",
                        sequence=seed.get("sequence", index),
                        field=field,
                    )
            expected_snapshot_hash = _hash_payload(
                {
                    "major_code": seed.get("major_code"),
                    "review_purpose": seed.get("review_purpose"),
                    "scope_label": seed.get("scope_label"),
                    "score_summary": seed.get("score_summary"),
                    "matrix_sample": seed.get("matrix_sample"),
                }
            )
            if seed.get("target_snapshot_hash") and seed.get("target_snapshot_hash") != expected_snapshot_hash:
                add_issue(
                    "seedpack_item_snapshot_hash_mismatch",
                    "Seedpack item snapshot hash does not match item content.",
                    sequence=seed.get("sequence", index),
                )
            if seed.get("source_artifact"):
                seed_artifact_path = _resolved_reference(seed.get("source_artifact"), base_path=seedpack_path)
                if seed_artifact_path not in loaded_artifact_paths:
                    add_issue(
                        "seedpack_source_artifact_not_in_run",
                        "Seedpack item source artifact is not part of the audited major run.",
                        sequence=seed.get("sequence", index),
                        source_artifact=seed.get("source_artifact"),
                    )

    if spotcheck_plan_path:
        spotcheck = _load_json(spotcheck_plan_path)
        if spotcheck.get("schema") != SPOTCHECK_PLAN_SCHEMA_VERSION:
            add_issue(
                "spotcheck_schema_mismatch",
                "Spotcheck plan schema is invalid.",
                schema=spotcheck.get("schema"),
            )
        items = spotcheck.get("items") or []
        counts["spotcheck_count"] = len(items)
        if spotcheck.get("spotcheck_count") != len(items):
            add_issue(
                "spotcheck_count_mismatch",
                "Spotcheck plan count does not match item count.",
                expected=spotcheck.get("spotcheck_count"),
                actual=len(items),
            )
        if seedpack_path:
            batch, seeds = _load_seedpack_records(seedpack_path)
            if spotcheck.get("seedpack_id") != batch.get("seedpack_id"):
                add_issue(
                    "spotcheck_seedpack_id_mismatch",
                    "Spotcheck plan seedpack id does not match seedpack.",
                    expected=batch.get("seedpack_id"),
                    actual=spotcheck.get("seedpack_id"),
                )
            if len(items) != len(seeds):
                add_issue(
                    "spotcheck_seed_count_mismatch",
                    "Spotcheck item count does not match seed count.",
                    expected=len(seeds),
                    actual=len(items),
                )
        for index, item in enumerate(items, start=1):
            for field in ("decision", "reviewer_id", "reviewed_at", "rationale"):
                if _clean(item.get(field)):
                    add_issue(
                        "spotcheck_decision_field_not_blank",
                        "Spotcheck decision/reviewer fields must remain blank.",
                        seed_sequence=item.get("seed_sequence", index),
                        field=field,
                    )
            if not item.get("search_queries"):
                add_issue(
                    "spotcheck_search_queries_missing",
                    "Spotcheck item has no search queries.",
                    seed_sequence=item.get("seed_sequence", index),
                )

    if method_work_queue_path:
        if not method_work_queue_path.exists():
            add_issue(
                "method_work_queue_missing",
                "Method work queue artifact does not exist.",
                path=str(method_work_queue_path),
            )
        else:
            try:
                method_queue = _load_json(method_work_queue_path)
                if method_queue.get("schema") != METHOD_WORK_QUEUE_SCHEMA_VERSION:
                    add_issue(
                        "method_work_queue_schema_mismatch",
                        "Method work queue schema is invalid.",
                        schema=method_queue.get("schema"),
                    )
                queue_items = method_queue.get("queue_items") or []
                counts["method_queue_count"] = len(queue_items)
                counts["method_queue_p0_count"] = sum(
                    1 for item in queue_items if item.get("priority") == "P0"
                )
                if method_queue.get("queue_count") != len(queue_items):
                    add_issue(
                        "method_work_queue_count_mismatch",
                        "Method work queue count does not match item count.",
                        expected=method_queue.get("queue_count"),
                        actual=len(queue_items),
                    )
                if seedpack_path:
                    batch, _ = _load_seedpack_records(seedpack_path)
                    queue_seedpack = (method_queue.get("seedpack_contract") or {}).get("seedpack_id")
                    if queue_seedpack != batch.get("seedpack_id"):
                        add_issue(
                            "method_work_queue_seedpack_mismatch",
                            "Method work queue seedpack id does not match seedpack.",
                            expected=batch.get("seedpack_id"),
                            actual=queue_seedpack,
                        )
                source_artifacts = method_queue.get("source_artifacts") or {}
                if seedpack_path and source_artifacts.get("review_seedpack") and not _same_reference(
                    source_artifacts.get("review_seedpack"),
                    str(seedpack_path),
                    left_base=method_work_queue_path,
                    right_base=seedpack_path,
                ):
                    add_issue(
                        "method_work_queue_seedpack_path_mismatch",
                        "Method work queue review seedpack path does not match audited seedpack.",
                        expected=str(seedpack_path),
                        actual=source_artifacts.get("review_seedpack"),
                    )
                for index, item in enumerate(queue_items, start=1):
                    major_code = _clean(item.get("major_code"))
                    if major_code and major_code != "ALL" and major_code not in loaded_major_codes:
                        add_issue(
                            "method_work_queue_major_not_in_run",
                            "Method work queue item references a major outside the audited run.",
                            queue_index=index,
                            major_code=major_code,
                            track=item.get("track"),
                        )
            except (OSError, json.JSONDecodeError) as exc:
                add_issue(
                    "method_work_queue_unreadable",
                    "Method work queue artifact could not be read.",
                    path=str(method_work_queue_path),
                    error=str(exc),
                )

    return {
        "schema": ARTIFACT_AUDIT_SCHEMA_VERSION,
        "generated_at": _now(),
        "ok": len(issues) == 0,
        "source_artifacts": {
            "major_run": str(major_run_path),
            "seedpack": str(seedpack_path) if seedpack_path else None,
            "spotcheck_plan": str(spotcheck_plan_path) if spotcheck_plan_path else None,
            "method_work_queue": str(method_work_queue_path) if method_work_queue_path else None,
        },
        "counts": counts,
        "issue_count": len(issues),
        "issues": issues,
    }


def build_ontology_transferability_release_gate(
    calibration_path: Path,
    method_work_queue_path: Path,
    *,
    artifact_audit_path: Path | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add_check(
        code: str,
        status: str,
        message: str,
        *,
        expected: Any = None,
        actual: Any = None,
    ) -> None:
        checks.append(
            {
                "code": code,
                "status": status,
                "message": message,
                "expected": expected,
                "actual": actual,
            }
        )

    calibration = _load_json(calibration_path)
    if calibration.get("schema") != CALIBRATION_SCHEMA_VERSION:
        add_check(
            "calibration_schema",
            "fail",
            "Calibration artifact schema is invalid.",
            expected=CALIBRATION_SCHEMA_VERSION,
            actual=calibration.get("schema"),
        )
    else:
        add_check(
            "calibration_schema",
            "pass",
            "Calibration artifact schema is valid.",
            expected=CALIBRATION_SCHEMA_VERSION,
            actual=calibration.get("schema"),
        )
    add_check(
        "calibration_ok",
        "pass" if calibration.get("ok") else "fail",
        "Calibration run must be complete before release.",
        expected=True,
        actual=calibration.get("ok"),
    )
    totals = calibration.get("totals") or {}
    manual_priority = int(totals.get("manual_priority_scope_count") or 0)
    no_course_link = int(totals.get("no_course_link_scope_count") or 0)
    add_check(
        "manual_priority_scope_backlog",
        "pass" if manual_priority == 0 else "fail",
        "Manual-priority scopes must be resolved before release.",
        expected=0,
        actual=manual_priority,
    )
    add_check(
        "no_course_link_scope_backlog",
        "pass" if no_course_link == 0 else "fail",
        "No-course-link scopes must be resolved or explicitly accepted before release.",
        expected=0,
        actual=no_course_link,
    )

    method_queue = _load_json(method_work_queue_path)
    if method_queue.get("schema") != METHOD_WORK_QUEUE_SCHEMA_VERSION:
        add_check(
            "method_queue_schema",
            "fail",
            "Method work queue schema is invalid.",
            expected=METHOD_WORK_QUEUE_SCHEMA_VERSION,
            actual=method_queue.get("schema"),
        )
    else:
        add_check(
            "method_queue_schema",
            "pass",
            "Method work queue schema is valid.",
            expected=METHOD_WORK_QUEUE_SCHEMA_VERSION,
            actual=method_queue.get("schema"),
        )
    validation_issues = method_queue.get("validation_issues") or []
    add_check(
        "method_queue_validation",
        "pass" if method_queue.get("ok") and not validation_issues else "fail",
        "Method work queue must have no contract validation issues.",
        expected=[],
        actual=validation_issues,
    )
    queue_items = method_queue.get("queue_items") or []
    p0_items = [item for item in queue_items if item.get("priority") == "P0"]
    add_check(
        "method_queue_open_items",
        "pass" if len(queue_items) == 0 else "fail",
        "Open method-work-queue items must be resolved before release.",
        expected=0,
        actual=len(queue_items),
    )
    add_check(
        "method_queue_p0_items",
        "pass" if len(p0_items) == 0 else "fail",
        "P0 method-work-queue items must be resolved before release.",
        expected=0,
        actual=len(p0_items),
    )

    artifact_audit: dict[str, Any] | None = None
    if artifact_audit_path:
        artifact_audit = _load_json(artifact_audit_path)
        if artifact_audit.get("schema") != ARTIFACT_AUDIT_SCHEMA_VERSION:
            add_check(
                "artifact_audit_schema",
                "fail",
                "Artifact audit schema is invalid.",
                expected=ARTIFACT_AUDIT_SCHEMA_VERSION,
                actual=artifact_audit.get("schema"),
            )
        else:
            add_check(
                "artifact_audit_schema",
                "pass",
                "Artifact audit schema is valid.",
                expected=ARTIFACT_AUDIT_SCHEMA_VERSION,
                actual=artifact_audit.get("schema"),
            )
        add_check(
            "artifact_audit_ok",
            "pass" if artifact_audit.get("ok") else "fail",
            "Structural artifact audit must pass before release.",
            expected=True,
            actual=artifact_audit.get("ok"),
        )
    else:
        add_check(
            "artifact_audit_required",
            "fail",
            "Artifact audit is required before release.",
            expected="artifact_audit_path",
            actual=None,
        )

    fail_count = sum(1 for check in checks if check.get("status") == "fail")
    warn_count = sum(1 for check in checks if check.get("status") == "warn")
    return {
        "schema": RELEASE_GATE_SCHEMA_VERSION,
        "generated_at": _now(),
        "ok": fail_count == 0,
        "status": "pass" if fail_count == 0 else "blocked",
        "source_artifacts": {
            "calibration": str(calibration_path),
            "method_work_queue": str(method_work_queue_path),
            "artifact_audit": str(artifact_audit_path) if artifact_audit_path else None,
        },
        "summary": {
            "pass_count": sum(1 for check in checks if check.get("status") == "pass"),
            "warn_count": warn_count,
            "fail_count": fail_count,
            "manual_priority_scope_count": manual_priority,
            "no_course_link_scope_count": no_course_link,
            "method_queue_count": len(queue_items),
            "method_queue_p0_count": len(p0_items),
            "artifact_audit_ok": artifact_audit.get("ok") if artifact_audit else None,
        },
        "checks": checks,
    }


def build_ontology_transferability_spotcheck_plan(seedpack_path: Path) -> dict[str, Any]:
    batch, seeds = _load_seedpack_records(seedpack_path)
    items: list[dict[str, Any]] = []
    evidence_checklist = [
        "job_scope_matches_public_or_official_source",
        "task_or_ksa_evidence_is_visible_beyond_title_similarity",
        "required_optional_grouping_is_plausible",
        "hours_methods_facilities_are_usable_for_delivery",
        "generic_or_duplicate_course_risk_is_not_hidden",
        "baseline_or_low_evidence_seed_remains_review_gated",
    ]
    for seed in seeds:
        scope_label = _clean(seed.get("scope_label"))
        hubs = [_clean(item) for item in (seed.get("top_hub_units") or []) if _clean(item)]
        scope_terms = [part.strip() for part in scope_label.split(">") if part.strip()]
        major_name = _clean(seed.get("major_name"))
        sub_scope = scope_terms[-1] if scope_terms else scope_label
        top_hub = hubs[0] if hubs else sub_scope
        search_queries = [
            f"NCS {sub_scope} 능력단위 교육과정",
            f"{sub_scope} NCS 훈련과정 시간 방법 시설",
            f"{top_hub} NCS 직무기술서",
            f"{major_name} {sub_scope} 교육체계",
        ]
        items.append(
            {
                "record_type": "spotcheck_item",
                "seedpack_id": batch.get("seedpack_id"),
                "seed_sequence": seed.get("sequence"),
                "major_code": seed.get("major_code"),
                "major_name": seed.get("major_name"),
                "review_purpose": seed.get("review_purpose"),
                "scope_label": scope_label,
                "score_summary": seed.get("score_summary"),
                "review_flags": seed.get("review_flags") or [],
                "top_hub_units": hubs[:5],
                "search_queries": search_queries,
                "preferred_sources": [
                    "NCS official ability-unit material",
                    "Work24 or official training-course detail",
                    "public job description using NCS units",
                    "qualification detail when the field has qualification linkage",
                    "public curriculum or internal education-system document",
                ],
                "evidence_checklist": evidence_checklist,
                "acceptance_rule": (
                    "High-support sample should be externally plausible; typical-middle sample should be "
                    "explainable; weak evidence must not be promoted to required, and no-course-link "
                    "rows must remain review-gated unless a human reviewer explicitly approves stronger use."
                ),
                "decision": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
            }
        )
    by_major: dict[str, dict[str, Any]] = {}
    for item in items:
        major_code = _clean(item.get("major_code"))
        major = by_major.setdefault(
            major_code,
            {
                "major_code": item.get("major_code"),
                "major_name": item.get("major_name"),
                "spotcheck_count": 0,
                "purposes": {},
                "flagged_count": 0,
            },
        )
        major["spotcheck_count"] += 1
        purpose = _clean(item.get("review_purpose"))
        major["purposes"][purpose] = int(major["purposes"].get(purpose) or 0) + 1
        if item.get("review_flags"):
            major["flagged_count"] += 1
    return {
        "schema": SPOTCHECK_PLAN_SCHEMA_VERSION,
        "generated_at": _now(),
        "source_seedpack": str(seedpack_path),
        "seedpack_id": batch.get("seedpack_id"),
        "ok": True,
        "spotcheck_count": len(items),
        "major_count": len(by_major),
        "items": items,
        "by_major": [by_major[key] for key in sorted(by_major)],
        "global_review_rule": (
            "Review one high-support, one typical-middle, and one baseline/low-evidence sample per major "
            "before accepting threshold changes."
        ),
    }


def write_ontology_transferability_json(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_ontology_transferability_field_review_json(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_ontology_transferability_field_review_markdown(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    totals = report.get("totals") or {}
    lines = [
        "# Ontology-Adjusted Field Review",
        "",
        f"- Schema: `{report.get('schema')}`",
        f"- Source run: `{report.get('source_run')}`",
        f"- OK: {report.get('ok')}",
        f"- Major count: {report.get('major_count')}",
        f"- Scope count: {totals.get('scope_count')}",
        f"- Unit count: {totals.get('unit_count')}",
        f"- Directed pair count: {totals.get('directed_pair_count')}",
        f"- Batch seconds: {totals.get('batch_seconds')}",
        "",
        "## Major Review Index",
        "",
        "| Major | Name | Scopes | Units | Top Scope | Top Avg Adj | Top Avg Exact | Top Adj-Exact | Top Baseline-heavy | Plausibility | Baseline-heavy Scopes | No-course Scopes |",
        "|---|---|---:|---:|---|---:|---:|---:|---:|---|---:|---:|",
    ]
    for item in report.get("major_reviews") or []:
        top_scope = item.get("top_scope") or {}
        lines.append(
            "| {major} | {name} | {scopes} | {units} | {top_scope} | {avg_adj:.4f} | {avg_exact:.4f} | {adj_exact:.4f} | {baseline:.4f} | {plausibility} | {baseline_count} | {no_course_count} |".format(
                major=item.get("major_code") or "",
                name=item.get("major_name") or "",
                scopes=int(item.get("scope_count") or 0),
                units=int(item.get("unit_count") or 0),
                top_scope=top_scope.get("scope_label") or "",
                avg_adj=float(item.get("top_avg_adjusted") or 0.0),
                avg_exact=float(item.get("top_avg_exact") or 0.0),
                adj_exact=float(item.get("top_avg_adjusted_minus_exact") or 0.0),
                baseline=float(item.get("top_baseline_heavy_pair_ratio") or 0.0),
                plausibility=item.get("plausibility") or "",
                baseline_count=int(item.get("baseline_heavy_scope_count") or 0),
                no_course_count=int(item.get("no_course_link_scope_count") or 0),
            )
        )
    lines.extend(["", "## Method Adjustments To Review", ""])
    for item in report.get("method_adjustment_candidates") or []:
        lines.append(f"- `{item.get('priority')}` `{item.get('topic')}`: {item.get('recommendation')}")
    lines.extend(
        [
            "",
            "## Review Rule",
            "",
            "For each major, review one high-support scope, one typical middle scope, and one baseline-heavy or low-evidence scope before accepting or changing thresholds.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ontology_transferability_review_seedpack_jsonl(seedpack: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        batch_record = {
            key: value
            for key, value in seedpack.items()
            if key not in {"seeds"}
        }
        handle.write(json.dumps(batch_record, ensure_ascii=False, sort_keys=True) + "\n")
        for seed in seedpack.get("seeds") or []:
            handle.write(json.dumps(seed, ensure_ascii=False, sort_keys=True) + "\n")


def write_ontology_transferability_review_seedpack_markdown(seedpack: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Ontology Transferability Review Seedpack",
        "",
        f"- Schema: `{seedpack.get('schema')}`",
        f"- seedpack_id: `{seedpack.get('seedpack_id')}`",
        f"- Source run: `{seedpack.get('source_run')}`",
        f"- Seeds: {seedpack.get('seed_count')}",
        f"- Failed majors: {seedpack.get('failed_major_count')}",
        f"- Allowed decisions: {', '.join(seedpack.get('allowed_decisions') or [])}",
        "- Decision fields are intentionally blank for human review.",
        "",
        "| Major | Name | Purpose | Scope | Avg Adj | Avg Exact | Baseline-heavy | Flags | Top Hub Units |",
        "|---|---|---|---|---:|---:|---:|---|---|",
    ]
    for seed in seedpack.get("seeds") or []:
        score = seed.get("score_summary") or {}
        lines.append(
            "| {major} | {name} | {purpose} | {scope} | {avg_adj:.4f} | {avg_exact:.4f} | {baseline:.4f} | {flags} | {hubs} |".format(
                major=seed.get("major_code") or "",
                name=seed.get("major_name") or "",
                purpose=seed.get("review_purpose") or "",
                scope=seed.get("scope_label") or "",
                avg_adj=_score_value(score, "avg_adjusted"),
                avg_exact=_score_value(score, "avg_exact"),
                baseline=_score_value(score, "baseline_heavy_pair_ratio"),
                flags=", ".join(seed.get("review_flags") or []),
                hubs=", ".join(seed.get("top_hub_units") or []),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ontology_transferability_calibration_json(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_ontology_transferability_method_work_queue_json(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_ontology_transferability_artifact_audit_json(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_ontology_transferability_release_gate_json(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_ontology_transferability_education_system_audit_json(
    report: dict[str, Any],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_ontology_transferability_course_link_gap_diagnostic_json(
    report: dict[str, Any],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_ontology_transferability_course_link_gap_diagnostic_markdown(
    report: dict[str, Any],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Ontology Transferability Course-Link Gap Diagnostic",
        "",
        f"- Schema: `{report.get('schema')}`",
        f"- Source run: `{report.get('source_run')}`",
        f"- OK: {report.get('ok')}",
        f"- Contract OK: {report.get('contract_ok')}",
        f"- Approval ready: {report.get('approval_ready')}",
        f"- Status: {report.get('status')}",
        f"- Human review required: {report.get('human_review_required')}",
        f"- Approval claim: {report.get('approval_claim')}",
        f"- DB writes: {report.get('db_writes')}",
        f"- Scope count: {report.get('scope_count')}",
        f"- Issue type counts: {report.get('issue_type_counts')}",
        "",
        "## Major Issue Counts",
        "",
        "| Major | Issue Type | Scope Count |",
        "|---|---|---:|",
    ]
    for row in report.get("major_issue_counts") or []:
        lines.append(
            "| {major} {name} | {issue} | {count} |".format(
                major=row.get("major_code") or "",
                name=row.get("major_name") or "",
                issue=row.get("issue_type") or "",
                count=int(row.get("scope_count") or 0),
            )
        )
    lines.extend(
        [
            "",
            "## Course-Link Gap Scopes",
            "",
            "| Major | Scope | Issue Type | Linked Units | Unlinked Units | Avg Adj | Avg Exact | Baseline-heavy | Sample Units |",
            "|---|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in report.get("scopes") or []:
        score = row.get("score_summary") or {}
        units = ", ".join(
            unit.get("unit_name") or ""
            for unit in (row.get("sample_units") or [])[:5]
            if unit.get("unit_name")
        )
        lines.append(
            "| {major} {name} | {scope} | {issue} | {linked} | {unlinked} | {adj:.4f} | {exact:.4f} | {baseline:.4f} | {units} |".format(
                major=row.get("major_code") or "",
                name=row.get("major_name") or "",
                scope=row.get("scope_label") or "",
                issue=row.get("issue_type") or "",
                linked=int(row.get("linked_unit_count") or 0),
                unlinked=int(row.get("unlinked_unit_count") or 0),
                adj=_score_value(score, "avg_adjusted"),
                exact=_score_value(score, "avg_exact"),
                baseline=_score_value(score, "baseline_heavy_pair_ratio"),
                units=units,
            )
        )
    lines.extend(["", "## Non-Mutation Note", "", report.get("non_mutation_note") or ""])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ontology_transferability_course_link_candidate_review_json(
    report: dict[str, Any],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_ontology_transferability_course_link_candidate_review_markdown(
    report: dict[str, Any],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Ontology Transferability Course-Link Candidate Review",
        "",
        f"- Schema: `{report.get('schema')}`",
        f"- Gap diagnostic: `{report.get('gap_diagnostic')}`",
        f"- Source run: `{report.get('source_run')}`",
        f"- OK: {report.get('ok')}",
        f"- Contract OK: {report.get('contract_ok')}",
        f"- Approval ready: {report.get('approval_ready')}",
        f"- Status: {report.get('status')}",
        f"- Human review required: {report.get('human_review_required')}",
        f"- Approval claim: {report.get('approval_claim')}",
        f"- DB writes: {report.get('db_writes')}",
        f"- Scope count: {report.get('scope_count')}",
        f"- Unit candidate count: {report.get('unit_candidate_count')}",
        f"- Course candidate count: {report.get('course_candidate_count')}",
        f"- Issue type counts: {report.get('issue_type_counts')}",
        f"- Candidate type counts: {report.get('candidate_type_counts')}",
        f"- Scope fit status counts: {report.get('scope_fit_status_counts')}",
        "",
        "## Review Candidates",
        "",
        "| Major | Scope | Issue Type | Review Action | Unit | Candidate Type | Scope Fit | Warnings | Course IDs | Course Names | Time | Methods | Facilities |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for scope in report.get("scopes") or []:
        for unit in scope.get("unit_candidates") or []:
            for group in unit.get("candidate_groups") or []:
                courses = group.get("courses") or []
                ids = ", ".join(str(course.get("training_course_id") or "") for course in courses)
                names = ", ".join(course.get("compe_unit_name") or "" for course in courses)
                times = ", ".join(course.get("train_time") or "" for course in courses if course.get("train_time"))
                methods = ", ".join(course.get("meth_name") or "" for course in courses if course.get("meth_name"))
                facilities = ", ".join(course.get("fac_name") or "" for course in courses if course.get("fac_name"))
                scope_fits = ", ".join(
                    (course.get("scope_fit") or {}).get("status") or ""
                    for course in courses
                    if (course.get("scope_fit") or {}).get("status")
                )
                warnings = ", ".join(
                    warning
                    for course in courses
                    for warning in ((course.get("scope_fit") or {}).get("warnings") or [])
                )
                lines.append(
                    "| {major} {major_name} | {scope_label} | {issue} | {review_action} | {unit_name} | {candidate_type} | {scope_fits} | {warnings} | {ids} | {names} | {times} | {methods} | {facilities} |".format(
                        major=scope.get("major_code") or "",
                        major_name=scope.get("major_name") or "",
                        scope_label=scope.get("scope_label") or "",
                        issue=scope.get("issue_type") or "",
                        review_action=scope.get("review_action") or "",
                        unit_name=unit.get("unit_name") or unit.get("unit_code") or "",
                        candidate_type=group.get("candidate_type") or "",
                        scope_fits=scope_fits,
                        warnings=warnings,
                        ids=ids,
                        names=names,
                        times=times,
                        methods=methods,
                        facilities=facilities,
                    )
                )
    if report.get("validation_issues"):
        lines.extend(["", "## Validation Issues", ""])
        for issue in report.get("validation_issues") or []:
            lines.append(f"- `{issue.get('code')}`: {issue.get('message')}")
    lines.extend(["", "## Non-Mutation Note", "", report.get("non_mutation_note") or ""])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ontology_transferability_calibration_markdown(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    totals = report.get("totals") or {}
    lines = [
        "# Ontology Transferability Calibration",
        "",
        f"- Schema: `{report.get('schema')}`",
        f"- Source run: `{report.get('source_run')}`",
        f"- Scope count: {totals.get('scope_count')}",
        f"- Matrix rows: {totals.get('matrix_row_count')}",
        f"- Matrix row course-link coverage: {totals.get('course_link_row_coverage')}",
        f"- Strong-support scopes: {totals.get('strong_support_scope_count')}",
        f"- Sample-review scopes: {totals.get('sample_review_scope_count')}",
        f"- Manual-priority scopes: {totals.get('manual_priority_scope_count')}",
        f"- No-course-link scopes: {totals.get('no_course_link_scope_count')}",
        "",
        "## Score Bands",
        "",
        "### Adjusted",
        "",
        "| Band | Scope Count |",
        "|---|---:|",
    ]
    for label, count in (report.get("band_counts") or {}).get("adjusted", {}).items():
        lines.append(f"| {label} | {count} |")
    lines.extend(["", "### Exact KSA", "", "| Band | Scope Count |", "|---|---:|"])
    for label, count in (report.get("band_counts") or {}).get("exact", {}).items():
        lines.append(f"| {label} | {count} |")
    lines.extend(["", "## Sensitivity", "", "### Baseline-heavy Pair Ratio", "", "| Threshold | Scope Count |", "|---:|---:|"])
    for threshold, count in (report.get("sensitivity") or {}).get("baseline_heavy_pair_ratio_thresholds", {}).items():
        lines.append(f"| {threshold} | {count} |")
    lines.extend(["", "### Low Exact / High Adjusted", "", "| Exact Floor | Scope Count |", "|---:|---:|"])
    for floor, count in (report.get("sensitivity") or {}).get("low_exact_high_adjusted_floors", {}).items():
        lines.append(f"| {floor} | {count} |")
    lines.extend(
        [
            "",
            "## Major Policy Buckets",
            "",
            "| Major | Name | Scopes | Strong | Sample Review | Manual Priority | No Course Links | Course Row Coverage |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in report.get("major_rows") or []:
        lines.append(
            "| {major} | {name} | {scopes} | {strong} | {sample} | {manual} | {no_course} | {coverage:.4f} |".format(
                major=item.get("major_code") or "",
                name=item.get("major_name") or "",
                scopes=int(item.get("scope_count") or 0),
                strong=int(item.get("strong_support_scope_count") or 0),
                sample=int(item.get("sample_review_scope_count") or 0),
                manual=int(item.get("manual_priority_scope_count") or 0),
                no_course=int(item.get("no_course_link_scope_count") or 0),
                coverage=float(item.get("course_link_row_coverage") or 0.0),
            )
        )
    lines.extend(["", "## Provisional Policy", ""])
    for item in report.get("provisional_policy") or []:
        lines.append(f"- `{item.get('bucket')}`: {item.get('rule')} -> {item.get('use')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ontology_transferability_artifact_audit_markdown(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = report.get("counts") or {}
    lines = [
        "# Ontology Transferability Artifact Audit",
        "",
        f"- Schema: `{report.get('schema')}`",
        f"- OK: {report.get('ok')}",
        f"- Issue count: {report.get('issue_count')}",
        f"- Major results: {counts.get('major_result_count')}",
        f"- Loaded majors: {counts.get('loaded_major_count')}",
        f"- Scopes: {counts.get('scope_count')}",
        f"- Matrix rows: {counts.get('matrix_row_count')}",
        f"- Recommended path stages: {counts.get('recommended_path_count')}",
        f"- Seeds: {counts.get('seed_count')}",
        f"- Spot-check items: {counts.get('spotcheck_count')}",
        f"- Method queue items: {counts.get('method_queue_count')}",
        f"- Method queue P0 items: {counts.get('method_queue_p0_count')}",
        "",
        "## Issues",
        "",
    ]
    issues = report.get("issues") or []
    if not issues:
        lines.append("- None")
    else:
        lines.extend(["| Code | Message | Context |", "|---|---|---|"])
        for issue in issues[:200]:
            context = {
                key: value
                for key, value in issue.items()
                if key not in {"code", "message"}
            }
            lines.append(
                "| {code} | {message} | {context} |".format(
                    code=issue.get("code") or "",
                    message=issue.get("message") or "",
                    context=json.dumps(context, ensure_ascii=False, sort_keys=True),
                )
            )
        if len(issues) > 200:
            lines.append(f"| truncated | Showing first 200 of {len(issues)} issues. | {{}} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ontology_transferability_release_gate_markdown(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = report.get("summary") or {}
    lines = [
        "# Ontology Transferability Release Gate",
        "",
        f"- Schema: `{report.get('schema')}`",
        f"- OK: {report.get('ok')}",
        f"- Status: `{report.get('status')}`",
        f"- Pass: {summary.get('pass_count')}",
        f"- Warn: {summary.get('warn_count')}",
        f"- Fail: {summary.get('fail_count')}",
        f"- Manual-priority scopes: {summary.get('manual_priority_scope_count')}",
        f"- No-course-link scopes: {summary.get('no_course_link_scope_count')}",
        f"- Method queue items: {summary.get('method_queue_count')}",
        f"- Method queue P0 items: {summary.get('method_queue_p0_count')}",
        "",
        "## Checks",
        "",
        "| Status | Code | Expected | Actual | Message |",
        "|---|---|---|---|---|",
    ]
    for check in report.get("checks") or []:
        lines.append(
            "| {status} | {code} | {expected} | {actual} | {message} |".format(
                status=check.get("status") or "",
                code=check.get("code") or "",
                expected=json.dumps(check.get("expected"), ensure_ascii=False, sort_keys=True),
                actual=json.dumps(check.get("actual"), ensure_ascii=False, sort_keys=True),
                message=check.get("message") or "",
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ontology_transferability_education_system_audit_markdown(
    report: dict[str, Any],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    aggregate = report.get("aggregate") or {}
    lines = [
        "# Ontology Transferability Education-System Audit",
        "",
        f"- Schema: `{report.get('schema')}`",
        f"- OK: {report.get('ok')}",
        f"- Contract OK: {report.get('contract_ok')}",
        f"- Approval ready: {report.get('approval_ready')}",
        f"- Status: `{report.get('status')}`",
        f"- Source run: `{report.get('source_run')}`",
        f"- Majors: {aggregate.get('major_count')} / failed {report.get('failed_major_count')}",
        f"- Scopes: {aggregate.get('scope_count')}",
        f"- Matrix rows: {aggregate.get('matrix_row_count')}",
        f"- Recommended path stages: {aggregate.get('recommended_path_stage_count')}",
        f"- Course-link row coverage: {aggregate.get('course_link_row_coverage')}",
        f"- Rows requiring human review: {aggregate.get('rows_requiring_human_review')}",
        f"- Rows without course links: {aggregate.get('matrix_rows_without_course_links')}",
        f"- Baseline-heavy rows: {aggregate.get('rows_with_baseline_heavy_flag')}",
        f"- Low-exact rows: {aggregate.get('rows_with_low_exact_flag')}",
        f"- Unsafe review statuses: {aggregate.get('unsafe_review_status_count')}",
        f"- Invalid review statuses: {aggregate.get('invalid_review_status_count')}",
        f"- Approval claim: {aggregate.get('approval_claim')}",
        f"- DB writes: {aggregate.get('db_writes')}",
        f"- Guide role: `{aggregate.get('guide_role')}`",
        f"- Review gate: `{(report.get('review_gate') or {}).get('status')}`",
        "",
        "## Guide Alignment",
        "",
        "| Stage | Surface | Evidence | Count |",
        "|---|---|---|---:|",
    ]
    for stage, item in (report.get("guide_alignment") or {}).items():
        count = item.get("stage_count", item.get("row_count", item.get("delivery_rows", "")))
        lines.append(
            "| {stage} | {surface} | {evidence} | {count} |".format(
                stage=stage,
                surface=item.get("surface") or "",
                evidence=item.get("evidence") or "",
                count=count,
            )
        )
    lines.extend(
        [
            "",
            "## Major Summary",
            "",
            "| Major | Name | Scopes | Matrix Rows | Course Coverage | Review Rows | No Course Rows | Baseline Heavy | Low Exact | Invalid Review |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in report.get("major_rows") or []:
        lines.append(
            "| {major} | {name} | {scopes} | {rows} | {coverage:.4f} | {review} | {no_course} | {baseline} | {low_exact} | {invalid} |".format(
                major=item.get("major_code") or "",
                name=item.get("major_name") or "",
                scopes=int(item.get("scope_count") or 0),
                rows=int(item.get("matrix_row_count") or 0),
                coverage=float(item.get("course_link_row_coverage") or 0.0),
                review=int(item.get("rows_requiring_human_review") or 0),
                no_course=int(item.get("matrix_rows_without_course_links") or 0),
                baseline=int(item.get("rows_with_baseline_heavy_flag") or 0),
                low_exact=int(item.get("rows_with_low_exact_flag") or 0),
                invalid=int(item.get("invalid_review_status_count") or 0),
            )
        )
    lines.extend(
        [
            "",
            "## Priority Scopes",
            "",
            "| Priority | Major | Scope | Review Rows | No Course Rows | Flags | Avg Adjusted | Avg Exact | Top Hubs |",
            "|---:|---|---|---:|---:|---|---:|---:|---|",
        ]
    )
    priority_scopes = report.get("priority_scopes") or []
    if not priority_scopes:
        lines.append("| 0 |  | None | 0 | 0 |  | 0 | 0 |  |")
    for item in priority_scopes[:25]:
        lines.append(
            "| {priority} | {major} {name} | {scope} | {review} | {no_course} | {flags} | {avg_adjusted:.4f} | {avg_exact:.4f} | {hubs} |".format(
                priority=int(item.get("priority_score") or 0),
                major=item.get("major_code") or "",
                name=item.get("major_name") or "",
                scope=item.get("scope_label") or "",
                review=int(item.get("rows_requiring_human_review") or 0),
                no_course=int(item.get("rows_without_course_links") or 0),
                flags=", ".join(item.get("review_flags") or []),
                avg_adjusted=float(item.get("avg_adjusted") or 0.0),
                avg_exact=float(item.get("avg_exact") or 0.0),
                hubs=", ".join(item.get("top_hub_units") or []),
            )
        )
    lines.extend(["", "## Findings", ""])
    findings = report.get("findings") or []
    if not findings:
        lines.append("- None")
    else:
        lines.extend(["| Severity | Code | Message | Context |", "|---|---|---|---|"])
        for finding in findings:
            context = {
                key: value
                for key, value in finding.items()
                if key not in {"severity", "code", "message"}
            }
            lines.append(
                "| {severity} | {code} | {message} | {context} |".format(
                    severity=finding.get("severity") or "",
                    code=finding.get("code") or "",
                    message=finding.get("message") or "",
                    context=json.dumps(context, ensure_ascii=False, sort_keys=True),
                )
            )
    lines.extend(["", "## Recommended Next Actions", ""])
    for action in report.get("recommended_next_actions") or []:
        lines.append(f"- {action}")
    lines.extend(["", "## Non-Mutation Note", "", report.get("non_mutation_note") or ""])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ontology_transferability_method_work_queue_markdown(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    totals = report.get("totals") or {}
    seedpack = report.get("seedpack_contract") or {}
    course_gap_summary = report.get("course_link_gap_diagnostic_summary") or {}
    lines = [
        "# Ontology Transferability Method Work Queue",
        "",
        f"- Schema: `{report.get('schema')}`",
        f"- OK: {report.get('ok')}",
        f"- Queue items: {report.get('queue_count')}",
        f"- Source calibration: `{(report.get('source_artifacts') or {}).get('calibration')}`",
        f"- Seedpack: `{seedpack.get('seedpack_id')}` ({seedpack.get('seed_count')} seeds, failed majors {seedpack.get('failed_major_count')})",
        f"- Manual-priority scopes: {totals.get('manual_priority_scope_count')}",
        f"- No-course-link scopes: {totals.get('no_course_link_scope_count')}",
        f"- Diagnostic course-link gap scopes: {course_gap_summary.get('scope_count')}",
        f"- Diagnostic issue counts: {course_gap_summary.get('issue_type_counts')}",
        "",
        "## Validation Issues",
        "",
    ]
    validation_issues = report.get("validation_issues") or []
    if not validation_issues:
        lines.append("- None")
    else:
        lines.extend(["| Code | Message | Context |", "|---|---|---|"])
        for issue in validation_issues:
            context = {
                key: value
                for key, value in issue.items()
                if key not in {"code", "message"}
            }
            lines.append(
                "| {code} | {message} | {context} |".format(
                    code=issue.get("code") or "",
                    message=issue.get("message") or "",
                    context=json.dumps(context, ensure_ascii=False, sort_keys=True),
                )
            )
    lines.extend(
        [
            "",
        "## Queue",
        "",
        "| Priority | Track | Major | Reason | Action |",
        "|---|---|---|---|---|",
        ]
    )
    for item in report.get("queue_items") or []:
        lines.append(
            "| {priority} | {track} | {major} {name} | {reason} | {action} |".format(
                priority=item.get("priority") or "",
                track=item.get("track") or "",
                major=item.get("major_code") or "",
                name=item.get("major_name") or "",
                reason=item.get("reason") or "",
                action=item.get("recommended_action") or "",
            )
        )
    lines.extend(["", "## Acceptance Rules", ""])
    for rule in report.get("acceptance_rules") or []:
        lines.append(f"- {rule}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ontology_transferability_spotcheck_plan_json(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_ontology_transferability_spotcheck_plan_markdown(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Ontology Transferability External Spot-Check Plan",
        "",
        f"- Schema: `{report.get('schema')}`",
        f"- Source seedpack: `{report.get('source_seedpack')}`",
        f"- Seedpack ID: `{report.get('seedpack_id')}`",
        f"- Spot-check items: {report.get('spotcheck_count')}",
        f"- Major count: {report.get('major_count')}",
        f"- Rule: {report.get('global_review_rule')}",
        "",
        "## Major Summary",
        "",
        "| Major | Name | Items | Flagged | Purposes |",
        "|---|---|---:|---:|---|",
    ]
    for item in report.get("by_major") or []:
        lines.append(
            "| {major} | {name} | {count} | {flagged} | {purposes} |".format(
                major=item.get("major_code") or "",
                name=item.get("major_name") or "",
                count=int(item.get("spotcheck_count") or 0),
                flagged=int(item.get("flagged_count") or 0),
                purposes=json.dumps(item.get("purposes") or {}, ensure_ascii=False, sort_keys=True),
            )
        )
    lines.extend(
        [
            "",
            "## Spot-Check Items",
            "",
            "| Major | Purpose | Scope | Flags | Search Queries | Checklist |",
            "|---|---|---|---|---|---|",
        ]
    )
    for item in report.get("items") or []:
        lines.append(
            "| {major} | {purpose} | {scope} | {flags} | {queries} | {checklist} |".format(
                major=f"{item.get('major_code') or ''} {item.get('major_name') or ''}",
                purpose=item.get("review_purpose") or "",
                scope=item.get("scope_label") or "",
                flags=", ".join(item.get("review_flags") or []),
                queries="<br>".join(item.get("search_queries") or []),
                checklist="<br>".join(item.get("evidence_checklist") or []),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ontology_transferability_pairs_csv(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "scope_label",
        "classification_id",
        "source_unit_code",
        "source_unit_name",
        "target_unit_code",
        "target_unit_name",
        "ontology_adjusted_transferability_ratio",
        "exact_ksa_overlap_ratio",
        "adjusted_minus_exact",
        "baseline_dependency_ratio",
        "ontology_related_ksa_count",
        "task_similarity_max_score",
        "task_similarity_link_count",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for scope in report.get("scopes") or []:
            classification = scope.get("classification") or {}
            rows = []
            matrix = scope.get("education_system", {}).get("training_system_matrix") or []
            matrix_names = {row.get("unit_code"): row.get("unit_name") for row in matrix}
            for pair in scope.get("top_undirected_pairs") or []:
                rows.append(pair)
            for pair in rows:
                row = {
                        "scope_label": scope.get("scope_label"),
                        "classification_id": classification.get("classification_id"),
                        "source_unit_code": pair.get("unit_a_code"),
                        "source_unit_name": matrix_names.get(pair.get("unit_a_code"), pair.get("unit_a")),
                        "target_unit_code": pair.get("unit_b_code"),
                        "target_unit_name": matrix_names.get(pair.get("unit_b_code"), pair.get("unit_b")),
                        "ontology_adjusted_transferability_ratio": pair.get("mean_adjusted"),
                        "exact_ksa_overlap_ratio": pair.get("mean_exact"),
                        "adjusted_minus_exact": pair.get("mean_adjusted_minus_exact"),
                        "baseline_dependency_ratio": pair.get("mean_baseline_dependency_ratio"),
                        "ontology_related_ksa_count": "",
                        "task_similarity_max_score": "",
                        "task_similarity_link_count": "",
                    }
                writer.writerow({field: _csv_cell(row.get(field)) for field in fields})


def write_ontology_transferability_markdown(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = report.get("summary") or {}
    lines = [
        "# NCS Ontology-Adjusted Education Systems",
        "",
        f"- Schema: `{report.get('schema')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Scope count: {summary.get('scope_count')}",
        f"- Unit count: {summary.get('unit_count')}",
        f"- Directed pair count: {summary.get('directed_pair_count')}",
        f"- Skipped scope count: {summary.get('skipped_scope_count')}",
        "",
        "## Method",
        "",
        "- Uses exact KSA overlap, same-sub-classification baseline, ontology concept relations, task similarity, job-base overlap, and qualification overlap.",
        "- Exact KSA overlap remains an audit signal; ontology-adjusted ratio is the planning signal.",
        "- `avg_adjusted_minus_exact` and `baseline_heavy_pair_ratio` flag cases where the score depends more on baseline/ontology expansion than direct exact KSA overlap.",
        "- Every generated education-system row remains `needs_review`; automation does not mark human review decisions.",
        "- Pair CSV artifacts contain each scope's top pair summaries, not every directed pair.",
        "",
        "## Top Scopes By Average Adjusted Transferability",
        "",
        "| Rank | Scope | Units | Avg Adjusted | Avg Exact | Adj-Exact | Baseline-heavy Pairs | Top Hub Units |",
        "|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for index, scope in enumerate((report.get("scopes") or [])[:30], start=1):
        groups = (scope.get("education_system") or {}).get("groups") or {}
        hubs = ", ".join(item.get("unit_name", "") for item in groups.get("common_transfer_hub", [])[:5])
        score = scope.get("score_summary") or {}
        lines.append(
            "| {rank} | {scope_label} | {units} | {avg_adjusted:.4f} | {avg_exact:.4f} | {adj_minus:.4f} | {baseline_heavy:.4f} | {hubs} |".format(
                rank=index,
                scope_label=scope.get("scope_label") or "",
                units=scope.get("unit_count") or 0,
                avg_adjusted=float(score.get("avg_adjusted") or 0.0),
                avg_exact=float(score.get("avg_exact") or 0.0),
                adj_minus=float(score.get("avg_adjusted_minus_exact") or 0.0),
                baseline_heavy=float(score.get("baseline_heavy_pair_ratio") or 0.0),
                hubs=hubs,
            )
        )
    lines.extend(["", "## Human Review Flags", ""])
    lines.extend(
        [
            "- Confirm whether the 0.24 same-sub-classification baseline is appropriate for each NCS domain.",
            "- Review low exact-overlap scopes whose adjusted score is mostly baseline-driven.",
            "- Confirm required/optional grouping before using the matrix as an official education system.",
            "- Link real internal/external courses by purpose, target, contents, hours, methods, facilities, and KSA evidence before operation.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
