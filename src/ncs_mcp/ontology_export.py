from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ncs_mcp.db import connect, initialize_database, now_utc


JSONLD_CONTEXT: dict[str, Any] = {
    "ncs": "https://ncs.go.kr/ontology/ncs#",
    "kqf": "https://ncs.go.kr/ontology/kqf#",
    "sqf": "https://ncs.go.kr/ontology/sqf#",
    "map": "https://ncs.go.kr/ontology/mapping#",
    "evidence": "https://ncs.go.kr/ontology/evidence#",
    "schema": "https://schema.org/",
    "name": "schema:name",
    "description": "schema:description",
    "code": "schema:identifier",
    "source": "schema:isBasedOn",
    "reviewStatus": "map:reviewStatus",
    "confidenceScore": "map:confidenceScore",
    "relation": "map:relation",
    "sourceNode": {"@id": "map:sourceNode", "@type": "@id"},
    "targetNode": {"@id": "map:targetNode", "@type": "@id"},
    "implements": {"@id": "kqf:implementedBy", "@type": "@id"},
    "hasSector": {"@id": "sqf:hasSector", "@type": "@id"},
    "hasJob": {"@id": "sqf:hasJob", "@type": "@id"},
    "hasJobLevel": {"@id": "sqf:hasJobLevel", "@type": "@id"},
    "hasRecognitionEvidence": {"@id": "sqf:hasRecognitionEvidence", "@type": "@id"},
}


def node_id(prefix: str, value: str) -> str:
    return f"{prefix}:{value}"


def export_ontology_jsonld(
    db_path: Path,
    out_path: Path,
    *,
    include_excluded_mappings: bool = False,
    include_chunk_evidence: bool = True,
    chunk_evidence_limit: int = 50000,
) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    graph: list[dict[str, Any]] = []
    try:
        graph.extend(
            [
                {
                    "@id": "kqf:KQF",
                    "@type": "kqf:QualificationFramework",
                    "name": "한국형 국가역량체계",
                    "description": (
                        "NCS 등을 바탕으로 학력, 자격, 현장경력, 교육훈련 이수 결과가 "
                        "상호 연계될 수 있도록 한 국가 수준의 수준체계."
                    ),
                    "implements": {"@id": "sqf:SQF"},
                },
                {
                    "@id": "sqf:SQF",
                    "@type": "sqf:SectoralQualificationFramework",
                    "name": "산업별역량체계",
                    "description": (
                        "산업별 현장에서 통용되는 직무를 도출하여 표준화하고, "
                        "직무수행에 필요한 능력을 구조화하여 교육훈련-학위-자격-현장경력을 "
                        "연계해 활용하는 체계."
                    ),
                },
                {
                    "@id": "sqf:RecognitionPathway",
                    "@type": "sqf:RecognitionEvidenceModel",
                    "name": "교육훈련-학위-자격-현장경력 연계",
                    "description": (
                        "SQF 직무수준은 교육훈련, 학위, 자격, 현장경력의 학습결과와 "
                        "직무역량을 연결하는 실무 단위로 사용된다."
                    ),
                },
            ]
        )

        for row in conn.execute(
            """
            SELECT sector_id, ncs_lclas_cd, ncs_lclas_name, sqf_field_name,
                   sqf_sub_field_name, sector_name, source_count
            FROM sqf_industry_sectors
            ORDER BY sector_id
            """
        ):
            graph.append(
                {
                    "@id": node_id("sqf-sector", row["sector_id"]),
                    "@type": "sqf:Sector",
                    "code": row["sector_id"],
                    "name": row["sector_name"],
                    "sqf:fieldName": row["sqf_field_name"],
                    "sqf:subFieldName": row["sqf_sub_field_name"],
                    "ncs:majorCode": row["ncs_lclas_cd"],
                    "ncs:majorName": row["ncs_lclas_name"],
                    "sqf:sourceCount": row["source_count"],
                }
            )

        for row in conn.execute(
            """
            SELECT j.sqf_job_id, j.sector_id, j.job_name, j.source_count
            FROM sqf_jobs_normalized j
            ORDER BY j.sqf_job_id
            """
        ):
            graph.append(
                {
                    "@id": node_id("sqf-job", row["sqf_job_id"]),
                    "@type": "sqf:Job",
                    "name": row["job_name"],
                    "sqf:inSector": {"@id": node_id("sqf-sector", row["sector_id"])},
                    "sqf:sourceCount": row["source_count"],
                }
            )

        for row in conn.execute(
            """
            SELECT jl.sqf_job_level_id, jl.sqf_job_id, jl.sqf_source_key,
                   jl.duty_name, jl.sqf_level, jl.level_name,
                   jl.job_level_definition, jl.duty_definition,
                   jl.autonomy_responsibility
            FROM sqf_job_levels_normalized jl
            ORDER BY jl.sqf_job_level_id
            """
        ):
            graph.append(
                {
                    "@id": node_id("sqf-job-level", row["sqf_job_level_id"]),
                    "@type": "sqf:JobLevel",
                    "code": row["sqf_source_key"],
                    "name": row["duty_name"],
                    "sqf:inJob": {"@id": node_id("sqf-job", row["sqf_job_id"])},
                    "sqf:level": row["sqf_level"],
                    "sqf:levelName": row["level_name"],
                    "description": row["duty_definition"] or row["job_level_definition"],
                    "sqf:autonomyResponsibility": row["autonomy_responsibility"],
                }
            )

        for row in conn.execute(
            """
            SELECT cu.unit_code, cu.unit_name_raw, cu.unit_level_raw,
                   cu.api_definition, c.major_code, c.major_name,
                   c.middle_code, c.middle_name, c.small_code, c.small_name,
                   c.sub_code, c.sub_name
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            ORDER BY cu.unit_code
            """
        ):
            graph.append(
                {
                    "@id": node_id("ncs-unit", row["unit_code"]),
                    "@type": "ncs:CompetencyUnit",
                    "code": row["unit_code"],
                    "name": row["unit_name_raw"],
                    "ncs:level": row["unit_level_raw"],
                    "description": row["api_definition"],
                    "ncs:classification": {
                        "majorCode": row["major_code"],
                        "majorName": row["major_name"],
                        "middleCode": row["middle_code"],
                        "middleName": row["middle_name"],
                        "smallCode": row["small_code"],
                        "smallName": row["small_name"],
                        "subCode": row["sub_code"],
                        "subName": row["sub_name"],
                    },
                }
            )

        mapping_where = "" if include_excluded_mappings else "WHERE m.filter_status = 'eligible'"
        for row in conn.execute(
            f"""
            SELECT m.match_id, m.source_id, m.target_id, m.relation, m.score,
                   m.confidence, m.match_method, m.evidence_text,
                   m.review_status, m.filter_status, m.exclusion_reason,
                   jl.sqf_job_level_id
            FROM sqf_ncs_matches m
            JOIN sqf_job_levels_normalized jl ON jl.sqf_source_key = m.source_id
            {mapping_where}
            ORDER BY m.match_id
            """
        ):
            graph.append(
                {
                    "@id": node_id("map", str(row["match_id"])),
                    "@type": "map:MappingCandidate",
                    "sourceNode": {"@id": node_id("sqf-job-level", row["sqf_job_level_id"])},
                    "targetNode": {"@id": node_id("ncs-unit", row["target_id"])},
                    "relation": row["relation"],
                    "confidenceScore": row["score"],
                    "map:confidenceType": row["confidence"],
                    "map:method": row["match_method"],
                    "map:evidenceText": row["evidence_text"],
                    "reviewStatus": row["review_status"],
                    "map:filterStatus": row["filter_status"],
                    "map:exclusionReason": row["exclusion_reason"],
                }
            )

        if include_chunk_evidence:
            for row in conn.execute(
                """
                SELECT m.match_id, m.sqf_job_level_id, m.relation, m.score,
                       m.method, m.evidence_text, m.review_status,
                       dc.chunk_id, dc.page_start, dc.page_end,
                       da.asset_name, ds.title
                FROM sqf_chunk_job_level_matches m
                JOIN sqf_document_chunks dc ON dc.chunk_id = m.chunk_id
                JOIN sqf_document_assets da ON da.asset_id = dc.asset_id
                JOIN sqf_document_sources ds ON ds.document_id = da.document_id
                WHERE m.review_status != 'rejected'
                ORDER BY m.score DESC, m.match_id
                LIMIT ?
                """,
                (chunk_evidence_limit,),
            ):
                graph.append(
                    {
                        "@id": node_id("evidence", str(row["match_id"])),
                        "@type": "evidence:DocumentEvidence",
                        "sourceNode": {"@id": node_id("evidence-chunk", str(row["chunk_id"]))},
                        "targetNode": {"@id": node_id("sqf-job-level", row["sqf_job_level_id"])},
                        "relation": row["relation"],
                        "confidenceScore": row["score"],
                        "map:method": row["method"],
                        "map:evidenceText": row["evidence_text"],
                        "reviewStatus": row["review_status"],
                        "evidence:documentTitle": row["title"],
                        "evidence:assetName": row["asset_name"],
                        "evidence:pageStart": row["page_start"],
                        "evidence:pageEnd": row["page_end"],
                    }
                )

        payload = {
            "@context": JSONLD_CONTEXT,
            "@id": "urn:ncs-sqf-ontology",
            "@type": "schema:Dataset",
            "name": "NCS-SQF Ontology Evidence Graph",
            "schema:dateModified": now_utc(),
            "@graph": graph,
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "out": str(out_path),
            "nodes_and_edges": len(graph),
            "include_excluded_mappings": include_excluded_mappings,
            "include_chunk_evidence": include_chunk_evidence,
            "chunk_evidence_limit": chunk_evidence_limit if include_chunk_evidence else 0,
        }
    finally:
        conn.close()


def validate_ontology_readiness(db_path: Path) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    try:
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in [
                "competency_units",
                "competency_elements",
                "performance_criteria",
                "ksa_items",
                "sqf_duties",
                "sqf_job_levels_normalized",
                "sqf_document_assets",
                "sqf_document_chunks",
                "sqf_chunk_job_level_matches",
                "sqf_ncs_matches",
            ]
        }
        issues: list[dict[str, Any]] = []
        unextracted_assets = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM sqf_document_assets
                WHERE extraction_status != 'extracted'
                """
            ).fetchone()[0]
        )
        if unextracted_assets:
            issues.append(
                {
                    "severity": "error",
                    "check": "document_extraction",
                    "detail": f"{unextracted_assets} SQF document assets are not extracted.",
                }
            )

        missing_job_levels = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM sqf_duties d
                LEFT JOIN sqf_job_levels_normalized jl ON jl.sqf_source_key = d.source_key
                WHERE jl.sqf_job_level_id IS NULL
                """
            ).fetchone()[0]
        )
        if missing_job_levels:
            issues.append(
                {
                    "severity": "error",
                    "check": "sqf_normalization",
                    "detail": f"{missing_job_levels} SQF API rows are not normalized as job levels.",
                }
            )

        eligible_mappings = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM sqf_ncs_matches
                WHERE filter_status = 'eligible'
                """
            ).fetchone()[0]
        )
        if eligible_mappings == 0:
            issues.append(
                {
                    "severity": "error",
                    "check": "sqf_ncs_mapping",
                    "detail": "No eligible SQF-NCS mapping candidates are available.",
                }
            )

        mapped_job_levels = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT source_id)
                FROM sqf_ncs_matches
                WHERE filter_status = 'eligible'
                """
            ).fetchone()[0]
        )
        total_job_levels = counts["sqf_job_levels_normalized"]
        mapping_coverage = round(mapped_job_levels / total_job_levels, 4) if total_job_levels else 0.0
        if mapping_coverage < 0.5:
            issues.append(
                {
                    "severity": "warning",
                    "check": "mapping_coverage",
                    "detail": f"Eligible SQF-NCS mapping coverage is {mapping_coverage:.1%}.",
                }
            )

        chunk_matched_job_levels = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT sqf_job_level_id)
                FROM sqf_chunk_job_level_matches
                WHERE review_status != 'rejected'
                """
            ).fetchone()[0]
        )
        chunk_evidence_coverage = (
            round(chunk_matched_job_levels / total_job_levels, 4) if total_job_levels else 0.0
        )
        if chunk_evidence_coverage < 0.3:
            issues.append(
                {
                    "severity": "warning",
                    "check": "chunk_evidence_coverage",
                    "detail": f"Document chunk evidence coverage is {chunk_evidence_coverage:.1%}.",
                }
            )

        return {
            "ok": not any(issue["severity"] == "error" for issue in issues),
            "counts": counts,
            "metrics": {
                "eligible_mappings": eligible_mappings,
                "mapped_job_levels": mapped_job_levels,
                "mapping_coverage": mapping_coverage,
                "chunk_matched_job_levels": chunk_matched_job_levels,
                "chunk_evidence_coverage": chunk_evidence_coverage,
            },
            "issues": issues,
            "note": "This validates readiness for evidence-based recommendation, not official SQF recognition.",
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Export and validate the NCS-SQF ontology graph.")
    parser.add_argument("action", choices=["export-jsonld", "validate"])
    parser.add_argument("--db", type=Path, default=Path("data/processed/ncs.db"))
    parser.add_argument("--out", type=Path, default=Path("exports/ncs_sqf_ontology.jsonld"))
    parser.add_argument("--include-excluded-mappings", action="store_true")
    parser.add_argument("--no-chunk-evidence", action="store_true")
    parser.add_argument("--chunk-evidence-limit", type=int, default=50000)
    args = parser.parse_args()
    if args.action == "validate":
        result = validate_ontology_readiness(args.db)
    else:
        result = export_ontology_jsonld(
            args.db,
            args.out,
            include_excluded_mappings=args.include_excluded_mappings,
            include_chunk_evidence=not args.no_chunk_evidence,
            chunk_evidence_limit=args.chunk_evidence_limit,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
