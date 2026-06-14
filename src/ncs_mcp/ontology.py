from __future__ import annotations

import re
import sqlite3
from typing import Any

from ncs_mcp.db import clamp_limit, now_utc, row_to_dict, rows_to_dicts
from ncs_mcp.mapping_policy import DEFAULT_MAPPING_FILTER, apply_mapping_filter, merge_filter_metadata


MVP_MAJOR_CODE = "02"
MVP_SQF_FIELD_NAME = "경영관리"
MVP_JOB_NAME = "경영지원"
MATCH_TARGET_TYPE = "ncs_competency_unit"
DEFAULT_REFINED_POLICY = "refined_if_approved"
SCOPE_MANAGEMENT_SUPPORT = "management_support"
SCOPE_BUSINESS_02 = "business_accounting_office_02"

ONTOLOGY_SCHEMA: dict[str, Any] = {
    "scope": "NCS-SQF ontology MCP",
    "purpose": {
        "policy_context": (
            "NCS를 기반으로 KQF/SQF가 지향하는 직무능력 중심 사회, "
            "교육훈련 품질 보장, 중복학습 완화, 평생경력개발경로 가시화를 지원한다."
        ),
        "service_goal": (
            "사용자가 원하는 업무를 물었을 때 SQF 직무수준, NCS 능력단위, "
            "수행준거, KSA, 교육훈련-학위-자격-현장경력 근거를 연결해 "
            "교육 추천과 역량 갭분석을 제공한다."
        ),
    },
    "mvp": {
        "major_code": MVP_MAJOR_CODE,
        "sqf_field_name": MVP_SQF_FIELD_NAME,
        "job_name": MVP_JOB_NAME,
    },
    "concepts": {
        "KQF": {
            "label": "한국형 국가역량체계",
            "definition": (
                "NCS 등을 바탕으로 학력, 자격, 현장경력, 교육훈련 이수 결과가 "
                "상호 연계될 수 있도록 한 국가 수준의 수준체계."
            ),
        },
        "SQF": {
            "label": "산업별역량체계",
            "definition": (
                "산업별 현장에서 통용되는 직무를 도출하여 표준화하고, "
                "직무수행에 필요한 능력을 구조화하여 교육훈련-학위-자격-현장경력을 "
                "연계해 활용하는 체계."
            ),
        },
        "Sector": {
            "label": "산업",
            "definition": "일반적인 근로자의 경력이동이 가능한 산업 활동분야 또는 영역.",
        },
        "Qualification": {
            "label": "역량",
            "definition": (
                "직업이나 특정 업무 수행에 필요한 자질, 소질, 능력이며 "
                "학위, 직업자격, 교육훈련 이수증 등 공식적으로 인정받은 역량을 포함한다."
            ),
        },
        "Framework": {
            "label": "체계",
            "definition": (
                "해당 산업과 관련된 학위, 자격, 교육훈련 등을 장착하기 위한 골격 또는 틀."
            ),
        },
        "SQFJob": {
            "label": "SQF 직무",
            "definition": (
                "업무수행에 필요한 지식과 기술이 유사하여 해당 노동시장에서 "
                "근로자의 수직적 경력이동이 일반적으로 이루어지는 업무의 집합."
            ),
        },
        "SQFLevel": {
            "label": "SQF 수준",
            "definition": (
                "업무수행에 필요한 지식 및 기술의 난이도와 복잡성에 따라 "
                "SQF 직무를 구분하는 기준."
            ),
        },
        "SQFJobLevel": {
            "label": "직무수준",
            "definition": (
                "SQF 직무를 SQF 수준에 따라 구분한 것으로, 직무에 요구되는 "
                "직무역량이 타 직무수준과 객관적으로 구분되는 일의 단위."
            ),
        },
        "JobCompetency": {
            "label": "직무역량",
            "definition": "특정 직무수준을 수행하기 위해 요구되는 지식, 기술, 자율성, 책임성 및 관련 능력.",
        },
    },
    "classes": [
        "KQF",
        "SQF",
        "NCSMajor",
        "NCSClassification",
        "NCSCompetencyUnit",
        "NCSCompetencyElement",
        "NCSPerformanceCriterion",
        "NCSKSA",
        "SQFSector",
        "SQFField",
        "SQFJob",
        "SQFLevel",
        "SQFJobLevel",
        "RecognitionEvidence",
        "DocumentEvidence",
        "MappingEvidence",
        "Recommendation",
    ],
    "relations": [
        "kqf:implementedBy",
        "sqf:hasSector",
        "ncs:hasClassification",
        "ncs:hasUnit",
        "ncs:hasElement",
        "ncs:hasPerformanceCriterion",
        "ncs:requiresKnowledge",
        "ncs:requiresSkill",
        "ncs:requiresAttitude",
        "sqf:hasJob",
        "sqf:hasLevel",
        "sqf:hasJobLevel",
        "sqf:hasRecognitionEvidence",
        "sqf:hasDocumentEvidence",
        "sqf:mappedToNCSMajor",
        "sqf:requiresNCSUnit",
        "sqf:partiallyCovers",
        "skos:closeMatch",
        "skos:related",
    ],
    "mapping_object": [
        "source_type",
        "source_id",
        "target_type",
        "target_id",
        "relation",
        "score",
        "confidence",
        "match_method",
        "evidence_text",
        "evidence_source",
        "source_version",
        "review_status",
        "scope_tag",
        "filter_status",
        "reviewer_notes",
    ],
    "principles": [
        "NCS-SQF is not modeled with sameAs by default.",
        "Every connection must keep evidence, confidence, source, and review status.",
        "Recommendations are evidence-based guidance, not official recognition decisions.",
    ],
}

STOPWORDS = {
    "수준",
    "직무",
    "업무",
    "관련",
    "위한",
    "수행",
    "필요",
    "능력",
    "있다",
    "한다",
    "그리고",
    "또는",
    "및",
}


def value(row: sqlite3.Row | dict[str, Any] | None, key: str, default: str = "") -> str:
    if row is None:
        return default
    try:
        raw = row[key]  # type: ignore[index]
    except (KeyError, IndexError):
        raw = row.get(key, default) if isinstance(row, dict) else default
    if raw is None:
        return default
    return str(raw)


def row_dict(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return row_to_dict(row)


def clean_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def has_direct_value(text: str | None) -> bool:
    normalized = clean_text(text)
    return bool(normalized) and normalized not in {"-", "없음", "해당없음", "N/A", "n/a"}


def normalize_duty_name(name: str | None) -> str:
    text = clean_text(name)
    text = re.sub(r"\([0-9]+\)$", "", text).strip()
    return text


def parse_int(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"\d+", str(text))
    return int(match.group(0)) if match else None


def tokens(*texts: str | None) -> set[str]:
    found: set[str] = set()
    for text in texts:
        for token in re.findall(r"[0-9A-Za-z가-힣]+", text or ""):
            token = token.strip()
            if len(token) >= 2 and token not in STOPWORDS:
                found.add(token)
    return found


def sqf_source_text(sqf_row: sqlite3.Row | dict[str, Any]) -> str:
    return " ".join(
        clean_text(value(sqf_row, key))
        for key in [
            "sqf_field_name",
            "sqf_sub_field_name",
            "job_name",
            "duty_name",
            "duty_level_name",
            "duty_level_definition",
            "duty_definition",
            "autonomy_responsibility",
        ]
    )


def unit_target_text(unit_row: sqlite3.Row | dict[str, Any]) -> str:
    return " ".join(
        clean_text(value(unit_row, key))
        for key in [
            "unit_name",
            "unit_name_raw",
            "api_definition",
            "major_name",
            "middle_name",
            "small_name",
            "sub_name",
        ]
    )


def sqf_summary(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "source_key": value(row, "source_key"),
        "ncs_lclas_cd": value(row, "ncs_lclas_cd"),
        "ncs_lclas_name": value(row, "ncs_lclas_name"),
        "sqf_field_name": value(row, "sqf_field_name"),
        "sqf_sub_field_name": value(row, "sqf_sub_field_name"),
        "job_name": value(row, "job_name"),
        "duty_name": value(row, "duty_name"),
        "duty_level": value(row, "duty_level"),
        "duty_level_name": value(row, "duty_level_name"),
        "duty_level_definition": value(row, "duty_level_definition"),
        "duty_definition": value(row, "duty_definition"),
    }


def unit_summary(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "unit_code": value(row, "unit_code"),
        "unit_name": value(row, "unit_name") or value(row, "unit_name_raw"),
        "unit_level": value(row, "unit_level") or value(row, "unit_level_raw"),
        "api_definition": value(row, "api_definition"),
        "classification": {
            "major_code": value(row, "major_code"),
            "major_name": value(row, "major_name"),
            "middle_name": value(row, "middle_name"),
            "small_name": value(row, "small_name"),
            "sub_name": value(row, "sub_name"),
        },
    }


def scope_tag_for_duty(row: sqlite3.Row | dict[str, Any], *, mvp_only: bool = False) -> str:
    if (
        mvp_only
        or (
            value(row, "ncs_lclas_cd") == MVP_MAJOR_CODE
            and value(row, "sqf_field_name") == MVP_SQF_FIELD_NAME
            and value(row, "job_name") == MVP_JOB_NAME
        )
    ):
        return SCOPE_MANAGEMENT_SUPPORT
    if value(row, "ncs_lclas_cd") == MVP_MAJOR_CODE:
        return SCOPE_BUSINESS_02
    major = value(row, "ncs_lclas_cd") or "unknown"
    return f"sqf_major_{major}"


def query_sqf_duties(
    conn: sqlite3.Connection,
    *,
    source_key: str | None = None,
    keyword: str | None = None,
    major_code: str | None = None,
    sqf_field_name: str | None = None,
    job_name: str | None = None,
    duty_name: str | None = None,
    duty_level: str | int | None = None,
    mvp_only: bool = False,
    limit: int = 50,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    if source_key:
        clauses.append("source_key = ?")
        params.append(source_key)
    if mvp_only:
        clauses.extend(
            [
                "ncs_lclas_cd = ?",
                "sqf_field_name = ?",
                "job_name = ?",
            ]
        )
        params.extend([MVP_MAJOR_CODE, MVP_SQF_FIELD_NAME, MVP_JOB_NAME])
    else:
        if major_code:
            clauses.append("ncs_lclas_cd = ?")
            params.append(major_code)
        if sqf_field_name:
            clauses.append("sqf_field_name = ?")
            params.append(sqf_field_name)
        if job_name:
            clauses.append("job_name = ?")
            params.append(job_name)
    if duty_name:
        clauses.append("(duty_name = ? OR duty_name LIKE ?)")
        params.extend([duty_name, f"{duty_name}(%)"])
    if duty_level is not None:
        clauses.append("duty_level = ?")
        params.append(str(duty_level))
    if keyword:
        pattern = f"%{keyword}%"
        clauses.append(
            """
            (
                sqf_field_name LIKE ?
                OR sqf_sub_field_name LIKE ?
                OR job_name LIKE ?
                OR duty_name LIKE ?
                OR duty_level_definition LIKE ?
                OR duty_definition LIKE ?
                OR autonomy_responsibility LIKE ?
                OR duty_education_training LIKE ?
                OR duty_qualification LIKE ?
            )
            """
        )
        params.extend([pattern] * 9)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"""
        SELECT *
        FROM sqf_duties
        {where}
        ORDER BY ncs_lclas_cd, sqf_field_name, job_name,
                 CAST(NULLIF(duty_level, '') AS INTEGER), duty_name, source_key
        LIMIT ?
        """,
        params + [clamp_limit(limit, default=50, maximum=5000)],
    ).fetchall()


def get_sqf_duty(conn: sqlite3.Connection, source_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sqf_duties WHERE source_key = ?", (source_key,)).fetchone()


def query_ncs_units_for_major(conn: sqlite3.Connection, major_code: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            cu.unit_code,
            cu.unit_name_raw AS unit_name,
            cu.unit_level_raw AS unit_level,
            cu.api_definition,
            c.major_code, c.major_name,
            c.middle_name, c.small_name, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE c.major_code = ?
        ORDER BY c.middle_code, c.small_code, c.sub_code, cu.unit_code
        """,
        (major_code,),
    ).fetchall()


def score_sqf_ncs_match(
    sqf_row: sqlite3.Row | dict[str, Any],
    unit_row: sqlite3.Row | dict[str, Any],
) -> dict[str, Any] | None:
    duty_base = normalize_duty_name(value(sqf_row, "duty_name"))
    unit_name = value(unit_row, "unit_name") or value(unit_row, "unit_name_raw")
    sub_name = value(unit_row, "sub_name")
    source_text = sqf_source_text(sqf_row)
    target_text = unit_target_text(unit_row)
    source_terms = tokens(source_text, duty_base)

    score = 0.0
    evidence: list[str] = []
    matched_terms: list[str] = []

    if duty_base:
        if duty_base == sub_name:
            score += 10.0
            evidence.append(f"SQF 직무명 '{value(sqf_row, 'duty_name')}'이 NCS 세분류 '{sub_name}'와 일치")
        elif duty_base in unit_name:
            score += 7.0
            evidence.append(f"SQF 직무명 '{duty_base}'이 NCS 능력단위명 '{unit_name}'에 포함")
        elif duty_base in target_text:
            score += 5.0
            evidence.append(f"SQF 직무명 '{duty_base}'이 NCS 설명 텍스트에 포함")

    for term in sorted(source_terms):
        if term in target_text:
            matched_terms.append(term)
            if term == duty_base:
                continue
            score += 1.5

    duty_level = parse_int(value(sqf_row, "duty_level"))
    unit_level = parse_int(value(unit_row, "unit_level") or value(unit_row, "unit_level_raw"))
    if duty_level is not None and unit_level is not None:
        if duty_level == unit_level:
            score += 2.0
            evidence.append(f"SQF 수준 {duty_level}과 NCS 능력단위 수준 {unit_level} 일치")
        elif abs(duty_level - unit_level) == 1:
            score += 1.0
            evidence.append(f"SQF 수준 {duty_level}과 NCS 능력단위 수준 {unit_level} 인접")

    if matched_terms:
        evidence.append("공통 핵심어: " + ", ".join(matched_terms[:8]))

    if score < 4.0:
        return None

    if score >= 12.0:
        relation = "closeMatch"
    elif score >= 7.0:
        relation = "partiallyCovers"
    else:
        relation = "related"

    return {
        "source": sqf_summary(sqf_row),
        "target": unit_summary(unit_row),
        "mapping": {
            "source_type": "sqf_duty",
            "source_id": value(sqf_row, "source_key"),
            "target_type": MATCH_TARGET_TYPE,
            "target_id": value(unit_row, "unit_code"),
            "relation": relation,
            "score": round(score, 2),
            "confidence": "lexical",
            "match_method": "sqf_ncs_text_v1",
            "evidence_text": "; ".join(evidence),
            "evidence_source": "SQF /openapi26 + local NCS DB",
            "source_version": "SQF openapi26; NCS local DB",
            "review_status": "candidate",
            "scope_tag": scope_tag_for_duty(sqf_row),
            "filter_status": "candidate",
        },
    }


def generate_mapping_candidates(
    conn: sqlite3.Connection,
    sqf_row: sqlite3.Row | dict[str, Any],
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    units = query_ncs_units_for_major(conn, value(sqf_row, "ncs_lclas_cd") or MVP_MAJOR_CODE)
    candidates = [
        candidate
        for unit in units
        if (candidate := score_sqf_ncs_match(sqf_row, unit)) is not None
    ]
    candidates.sort(
        key=lambda item: (
            -float(item["mapping"]["score"]),
            item["target"]["classification"]["sub_name"],
            item["target"]["unit_code"],
        )
    )
    return candidates[: clamp_limit(limit, default=10, maximum=100)]


def insert_mapping_candidate(
    conn: sqlite3.Connection,
    candidate: dict[str, Any],
    *,
    scope_tag: str | None = None,
) -> None:
    mapping = candidate["mapping"]
    timestamp = now_utc()
    scope = scope_tag or mapping.get("scope_tag") or SCOPE_BUSINESS_02
    filter_result = apply_mapping_filter([candidate])
    filter_status = "eligible" if filter_result["matches"] else "excluded"
    exclusion_reason = None
    if filter_result["excluded"]:
        exclusion_reason = filter_result["excluded"][0]["reason"]
    conn.execute(
        """
        INSERT INTO sqf_ncs_matches(
            source_type, source_id, target_type, target_id, relation,
            score, confidence, match_method, evidence_text, evidence_source,
            source_version, review_status, scope_tag, filter_status,
            exclusion_reason, created_by_method, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_type, source_id, target_type, target_id, relation, match_method)
        DO UPDATE SET
            score = excluded.score,
            confidence = excluded.confidence,
            evidence_text = excluded.evidence_text,
            evidence_source = excluded.evidence_source,
            source_version = excluded.source_version,
            scope_tag = COALESCE(NULLIF(sqf_ncs_matches.scope_tag, ''), excluded.scope_tag),
            filter_status = excluded.filter_status,
            exclusion_reason = excluded.exclusion_reason,
            created_by_method = excluded.created_by_method,
            updated_at = excluded.updated_at
        WHERE sqf_ncs_matches.review_status NOT IN ('human_reviewed', 'reviewed', 'accepted', 'rejected')
        """,
        (
            mapping["source_type"],
            mapping["source_id"],
            mapping["target_type"],
            mapping["target_id"],
            mapping["relation"],
            mapping["score"],
            mapping["confidence"],
            mapping["match_method"],
            mapping["evidence_text"],
            mapping["evidence_source"],
            mapping["source_version"],
            mapping["review_status"],
            scope,
            filter_status,
            exclusion_reason,
            mapping["match_method"],
            timestamp,
            timestamp,
        ),
    )


def build_sqf_mapping_candidates(
    conn: sqlite3.Connection,
    *,
    mvp_only: bool = True,
    major_code: str | None = None,
    keyword: str | None = None,
    source_key: str | None = None,
    limit_per_duty: int = 10,
    duty_limit: int = 5000,
) -> dict[str, Any]:
    duties = query_sqf_duties(
        conn,
        source_key=source_key,
        keyword=keyword,
        major_code=major_code,
        mvp_only=mvp_only,
        limit=duty_limit,
    )
    inserted_or_updated = 0
    candidates_total = 0
    scope_tag = SCOPE_MANAGEMENT_SUPPORT if mvp_only else None
    for duty in duties:
        candidates = generate_mapping_candidates(conn, duty, limit=limit_per_duty)
        candidates_total += len(candidates)
        for candidate in candidates:
            insert_mapping_candidate(
                conn,
                candidate,
                scope_tag=scope_tag or scope_tag_for_duty(duty, mvp_only=mvp_only),
            )
            inserted_or_updated += 1
    conn.commit()
    stored_total = conn.execute("SELECT COUNT(*) FROM sqf_ncs_matches").fetchone()[0]
    reviewed_total = conn.execute(
        """
        SELECT COUNT(*)
        FROM sqf_ncs_matches
        WHERE review_status IN ('human_reviewed', 'reviewed', 'accepted')
        """
    ).fetchone()[0]
    return {
        "scope": scope_tag or ("management_support_mvp" if mvp_only else "sqf"),
        "sqf_duties_seen": len(duties),
        "candidates_generated": candidates_total,
        "candidates_upserted": inserted_or_updated,
        "stored_matches_total": int(stored_total),
        "reviewed_matches_total": int(reviewed_total),
        "limit_per_duty": clamp_limit(limit_per_duty, default=10, maximum=100),
        "duty_limit": clamp_limit(duty_limit, default=5000, maximum=5000),
        "confidence": "lexical",
    }


def get_stored_matches(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    target_id: str | None = None,
    include_candidates: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses = ["m.source_type = 'sqf_duty'", "m.source_id = ?"]
    params: list[Any] = [source_id]
    if target_id:
        clauses.append("m.target_id = ?")
        params.append(target_id)
    if not include_candidates:
        clauses.append("m.review_status IN ('human_reviewed', 'reviewed', 'accepted')")
    rows = conn.execute(
        f"""
        SELECT
            m.*,
            sd.source_key AS source_key,
            sd.ncs_lclas_cd, sd.ncs_lclas_name, sd.sqf_field_name, sd.sqf_sub_field_name,
            sd.job_name, sd.duty_name, sd.duty_level, sd.duty_level_name,
            sd.duty_level_definition, sd.duty_definition,
            cu.unit_code AS unit_code,
            cu.unit_name_raw AS unit_name, cu.unit_level_raw AS unit_level, cu.api_definition,
            c.major_code, c.major_name, c.middle_name, c.small_name, c.sub_name
        FROM sqf_ncs_matches m
        JOIN sqf_duties sd ON sd.source_key = m.source_id
        LEFT JOIN competency_units cu ON cu.unit_code = m.target_id
        LEFT JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE {' AND '.join(clauses)}
        ORDER BY
            CASE WHEN m.review_status IN ('human_reviewed', 'reviewed', 'accepted') THEN 0 ELSE 1 END,
            m.score DESC, m.target_id
        LIMIT ?
        """,
        params + [clamp_limit(limit, default=50, maximum=200)],
    ).fetchall()
    result = []
    for row in rows:
        item = {
            "source": sqf_summary(row),
            "target": unit_summary(row),
            "mapping": {
                "match_id": row["match_id"],
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "relation": row["relation"],
                "score": row["score"],
                "confidence": row["confidence"],
                "match_method": row["match_method"],
                "evidence_text": row["evidence_text"],
                "evidence_source": row["evidence_source"],
                "source_version": row["source_version"],
                "review_status": row["review_status"],
                "scope_tag": value(row, "scope_tag"),
                "filter_status": value(row, "filter_status"),
                "exclusion_reason": value(row, "exclusion_reason"),
                "reviewer_notes": value(row, "reviewer_notes"),
            },
        }
        result.append(item)
    return result


def get_or_generate_matches(
    conn: sqlite3.Connection,
    sqf_row: sqlite3.Row | dict[str, Any],
    *,
    limit: int = 10,
) -> tuple[str, list[dict[str, Any]]]:
    stored = get_stored_matches(
        conn,
        source_id=value(sqf_row, "source_key"),
        include_candidates=True,
        limit=limit,
    )
    if stored:
        return "stored", stored
    return "generated_candidate", generate_mapping_candidates(conn, sqf_row, limit=limit)


def get_filtered_matches(
    conn: sqlite3.Connection,
    sqf_row: sqlite3.Row | dict[str, Any],
    *,
    limit: int = 10,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    mapping_status, matches = get_or_generate_matches(conn, sqf_row, limit=max(limit, 50))
    filtered = apply_mapping_filter(matches, DEFAULT_MAPPING_FILTER)
    return mapping_status, filtered["matches"][: clamp_limit(limit, default=10, maximum=100)], filtered["metadata"]


def direct_sqf_conditions(row: sqlite3.Row | dict[str, Any]) -> dict[str, str]:
    return {
        key: value(row, key)
        for key in [
            "duty_education_training",
            "duty_qualification",
            "duty_career",
            "duty_license",
            "duty_remark",
        ]
        if has_direct_value(value(row, key))
    }


def analyze_sqf_gap(
    conn: sqlite3.Connection,
    *,
    current_ncs_unit_codes: list[str],
    target_source_key: str | None = None,
    target_job_name: str = MVP_JOB_NAME,
    target_duty_name: str | None = None,
    target_level: int | str | None = None,
    mvp_only: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    if target_source_key:
        duties = query_sqf_duties(conn, source_key=target_source_key, limit=1)
    else:
        duties = query_sqf_duties(
            conn,
            job_name=target_job_name,
            duty_name=target_duty_name,
            duty_level=target_level,
            mvp_only=mvp_only,
            keyword=None if target_duty_name else target_job_name,
            limit=clamp_limit(limit, default=10, maximum=50),
        )
    if not duties:
        return {
            "error": "sqf_target_not_found",
            "target_job_name": target_job_name,
            "target_duty_name": target_duty_name,
            "target_level": target_level,
        }

    current = set(current_ncs_unit_codes or [])
    target_results: list[dict[str, Any]] = []
    required_units: dict[str, dict[str, Any]] = {}
    covered_codes: set[str] = set()

    for duty in duties:
        mapping_status, matches, filter_metadata = get_filtered_matches(conn, duty, limit=limit)
        target_matches = []
        for match in matches:
            unit_code = match["target"]["unit_code"]
            if not unit_code:
                continue
            required_units.setdefault(unit_code, match)
            if unit_code in current:
                covered_codes.add(unit_code)
            target_matches.append(match)
        target_results.append(
            {
                "sqf_duty": sqf_summary(duty),
                "direct_sqf_conditions": direct_sqf_conditions(duty),
                "mapping_status": mapping_status,
                "mapping_filter_metadata": filter_metadata,
                "ncs_matches": target_matches,
            }
        )

    required_codes = set(required_units)
    missing_codes = required_codes - current
    covered_units = [required_units[code] for code in sorted(covered_codes)]
    missing_units = [
        required_units[code]
        for code in sorted(
            missing_codes,
            key=lambda code: (
                -float(required_units[code]["mapping"]["score"]),
                code,
            ),
        )
    ]
    coverage_ratio = round(len(covered_codes) / len(required_codes), 2) if required_codes else 0.0
    return {
        "target": {
            "job_name": target_job_name,
            "duty_name": target_duty_name,
            "level": str(target_level) if target_level is not None else None,
            "mvp_scope": mvp_only,
        },
        "current_ncs_unit_codes": sorted(current),
        "coverage_ratio": coverage_ratio,
        "required_unit_count": len(required_codes),
        "covered_unit_count": len(covered_codes),
        "missing_unit_count": len(missing_codes),
        "covered_ncs_units": covered_units,
        "missing_ncs_units": missing_units,
        "targets": target_results,
        "metadata": {
            "data_source": "SQLite NCS/SQF knowledge base",
            "query_scope": "management_support" if mvp_only else "sqf",
            "used_refined_policy": DEFAULT_REFINED_POLICY,
            **merge_filter_metadata([item["mapping_filter_metadata"] for item in target_results]),
        },
        "note": "후보 매핑 기반 분석은 공식 인정 또는 평가 판정이 아니다.",
    }


def recommend_next_ncs_units(
    conn: sqlite3.Connection,
    *,
    current_ncs_unit_codes: list[str],
    target_source_key: str | None = None,
    target_job_name: str = MVP_JOB_NAME,
    target_duty_name: str | None = None,
    target_level: int | str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    gap = analyze_sqf_gap(
        conn,
        current_ncs_unit_codes=current_ncs_unit_codes,
        target_source_key=target_source_key,
        target_job_name=target_job_name,
        target_duty_name=target_duty_name,
        target_level=target_level,
        limit=max(limit, 10),
    )
    if "error" in gap:
        return gap
    next_units = gap["missing_ncs_units"][: clamp_limit(limit, default=5, maximum=20)]
    return {
        "target": gap["target"],
        "coverage_ratio": gap["coverage_ratio"],
        "next_ncs_units": next_units,
        "recommendation_basis": "missing NCS units sorted by SQF-NCS mapping score",
        "metadata": gap.get("metadata", {}),
    }


def build_learning_objectives_for_units(
    conn: sqlite3.Connection,
    unit_codes: list[str],
    *,
    limit_per_unit: int = 3,
) -> list[dict[str, Any]]:
    objectives: list[dict[str, Any]] = []
    for unit_code in unit_codes:
        unit = conn.execute(
            """
            SELECT
                cu.unit_code, cu.unit_name_raw AS unit_name, cu.unit_level_raw AS unit_level,
                cu.api_definition, c.major_code, c.major_name, c.middle_name, c.small_name, c.sub_name
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE cu.unit_code = ?
            """,
            (unit_code,),
        ).fetchone()
        if unit is None:
            continue
        element_rows = conn.execute(
            """
            SELECT *
            FROM competency_elements
            WHERE unit_code = ?
            ORDER BY CAST(element_no AS INTEGER), element_id
            LIMIT ?
            """,
            (unit_code, clamp_limit(limit_per_unit, default=3, maximum=10)),
        ).fetchall()
        elements: list[dict[str, Any]] = []
        for element in element_rows:
            criteria_rows = conn.execute(
                """
                SELECT criteria_no, criteria_text_raw
                FROM performance_criteria
                WHERE element_id = ?
                ORDER BY CAST(criteria_no AS INTEGER), criteria_id
                LIMIT 3
                """,
                (element["element_id"],),
            ).fetchall()
            ksa_rows = conn.execute(
                """
                SELECT ksa_type_name, ksa_no, ksa_text_raw
                FROM ksa_items
                WHERE element_id = ?
                ORDER BY ksa_type_code, CAST(ksa_no AS INTEGER), ksa_id
                LIMIT 6
                """,
                (element["element_id"],),
            ).fetchall()
            elements.append(
                {
                    "element_id": element["element_id"],
                    "element_no": element["element_no"],
                    "element_name": element["element_name_raw"],
                    "performance_criteria": rows_to_dicts(criteria_rows),
                    "ksa": rows_to_dicts(ksa_rows),
                }
            )
        objectives.append(
            {
                "unit": unit_summary(unit),
                "learning_objective": f"{unit['unit_name']} 능력단위의 수행준거와 KSA를 학습목표로 보완한다.",
                "elements": elements,
            }
        )
    return objectives


def explain_mapping(
    conn: sqlite3.Connection,
    *,
    sqf_source_key: str,
    ncs_unit_code: str,
) -> dict[str, Any]:
    stored = get_stored_matches(
        conn,
        source_id=sqf_source_key,
        target_id=ncs_unit_code,
        include_candidates=True,
        limit=10,
    )
    if stored:
        return {"mapping_status": "stored", "matches": stored}
    sqf_row = get_sqf_duty(conn, sqf_source_key)
    if sqf_row is None:
        return {"error": "sqf_target_not_found", "source_key": sqf_source_key}
    unit_row = conn.execute(
        """
        SELECT
            cu.unit_code,
            cu.unit_name_raw AS unit_name,
            cu.unit_level_raw AS unit_level,
            cu.api_definition,
            c.major_code, c.major_name, c.middle_name, c.small_name, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE cu.unit_code = ?
        """,
        (ncs_unit_code,),
    ).fetchone()
    if unit_row is None:
        return {"error": "ncs_unit_not_found", "unit_code": ncs_unit_code}
    candidate = score_sqf_ncs_match(sqf_row, unit_row)
    if candidate is None:
        return {
            "mapping_status": "not_matched",
            "source": sqf_summary(sqf_row),
            "target": unit_summary(unit_row),
            "note": "The lexical matcher did not find enough shared evidence.",
        }
    return {"mapping_status": "generated_candidate", "matches": [candidate]}


def search_sqf_jobs_summary(
    conn: sqlite3.Connection,
    *,
    keyword: str | None = None,
    major_code: str | None = MVP_MAJOR_CODE,
    mvp_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if mvp_only:
        clauses.extend(["ncs_lclas_cd = ?", "sqf_field_name = ?", "job_name = ?"])
        params.extend([MVP_MAJOR_CODE, MVP_SQF_FIELD_NAME, MVP_JOB_NAME])
    elif major_code:
        clauses.append("ncs_lclas_cd = ?")
        params.append(major_code)
    if keyword:
        pattern = f"%{keyword}%"
        clauses.append(
            """
            (
                sqf_field_name LIKE ?
                OR sqf_sub_field_name LIKE ?
                OR job_name LIKE ?
                OR duty_name LIKE ?
                OR duty_definition LIKE ?
            )
            """
        )
        params.extend([pattern] * 5)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT
            ncs_lclas_cd, ncs_lclas_name, sqf_field_name, sqf_sub_field_name,
            job_name, COUNT(*) AS duty_level_count,
            MIN(CAST(NULLIF(duty_level, '') AS INTEGER)) AS min_level,
            MAX(CAST(NULLIF(duty_level, '') AS INTEGER)) AS max_level
        FROM sqf_duties
        {where}
        GROUP BY ncs_lclas_cd, ncs_lclas_name, sqf_field_name, sqf_sub_field_name, job_name
        ORDER BY ncs_lclas_cd, sqf_field_name, job_name
        LIMIT ?
        """,
        params + [clamp_limit(limit, default=50, maximum=200)],
    ).fetchall()
    return rows_to_dicts(rows)
