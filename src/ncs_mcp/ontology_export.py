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
    "hasChunk": {"@id": "evidence:hasChunk", "@type": "@id"},
    "fromDocument": {"@id": "evidence:fromDocument", "@type": "@id"},
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
    include_document_chunks: bool = True,
    document_chunk_limit: int = 20000,
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

        if include_document_chunks:
            for row in conn.execute(
                """
                SELECT document_id, title, ontology_role, local_path,
                       content_hash, text_extraction_status
                FROM sqf_document_sources
                ORDER BY document_id
                """
            ):
                graph.append(
                    {
                        "@id": node_id("evidence-document", str(row["document_id"])),
                        "@type": "evidence:DocumentSource",
                        "name": row["title"],
                        "evidence:ontologyRole": row["ontology_role"],
                        "evidence:localPath": row["local_path"],
                        "evidence:contentHash": row["content_hash"],
                        "evidence:extractionStatus": row["text_extraction_status"],
                    }
                )

            for row in conn.execute(
                """
                SELECT dc.chunk_id, da.document_id, da.asset_name,
                       dc.chunk_index, dc.page_start, dc.page_end,
                       dc.text, dc.char_count, dc.keywords_json, dc.ontology_tags_json
                FROM sqf_document_chunks dc
                JOIN sqf_document_assets da ON da.asset_id = dc.asset_id
                ORDER BY dc.chunk_id
                LIMIT ?
                """,
                (document_chunk_limit,),
            ):
                text = row["text"] or ""
                graph.append(
                    {
                        "@id": node_id("evidence-chunk", str(row["chunk_id"])),
                        "@type": "evidence:DocumentChunk",
                        "fromDocument": {
                            "@id": node_id("evidence-document", str(row["document_id"]))
                        },
                        "evidence:assetName": row["asset_name"],
                        "evidence:chunkIndex": row["chunk_index"],
                        "evidence:pageStart": row["page_start"],
                        "evidence:pageEnd": row["page_end"],
                        "evidence:charCount": row["char_count"],
                        "evidence:keywords": row["keywords_json"],
                        "evidence:ontologyTags": row["ontology_tags_json"],
                        "evidence:snippet": text[:700],
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
            "include_document_chunks": include_document_chunks,
            "document_chunk_limit": document_chunk_limit if include_document_chunks else 0,
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
                "ontology_concepts",
                "ksa_atomic_items",
                "ksa_atomic_concept_links",
                "ksa_meaning_candidates",
                "task_ksa_concept_relations",
                "task_similarity_links",
                "ncs_training_courses",
                "ncs_training_course_unit_links",
                "ncs_training_course_concept_links",
                "ncs_training_course_element_links",
                "training_goal_concept_links",
                "training_delivery_relations",
            ]
        }
        issues: list[dict[str, Any]] = []

        unlinked_ksa_items = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM ksa_items ki
                LEFT JOIN ksa_concept_links link ON link.ksa_id = ki.ksa_id
                WHERE TRIM(COALESCE(ki.ksa_text_raw, '')) <> ''
                  AND link.link_id IS NULL
                """
            ).fetchone()[0]
        )
        if unlinked_ksa_items:
            issues.append(
                {
                    "severity": "error",
                    "check": "ksa_concept_links",
                    "detail": f"{unlinked_ksa_items} non-empty KSA rows are not linked to ontology concepts.",
                }
            )

        atomic_without_concepts = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM ksa_atomic_items atom
                LEFT JOIN ksa_atomic_concept_links link ON link.atomic_id = atom.atomic_id
                WHERE link.link_id IS NULL
                """
            ).fetchone()[0]
        )
        if atomic_without_concepts:
            issues.append(
                {
                    "severity": "error",
                    "check": "atomic_ksa_concept_links",
                    "detail": f"{atomic_without_concepts} atomic KSA rows are not linked to concepts.",
                }
            )

        tasks_with_similarity = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT source_criteria_id)
                FROM task_similarity_links
                """
            ).fetchone()[0]
        )
        task_similarity_coverage = (
            round(tasks_with_similarity / counts["performance_criteria"], 4)
            if counts["performance_criteria"]
            else 0.0
        )
        if counts["performance_criteria"] and task_similarity_coverage == 0:
            issues.append(
                {
                    "severity": "error",
                    "check": "task_similarity_links",
                    "detail": "No task similarity links are available.",
                }
            )

        concepts_with_meanings = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT concept_id)
                FROM ksa_meaning_candidates
                """
            ).fetchone()[0]
        )
        ksa_meaning_coverage = (
            round(concepts_with_meanings / counts["ontology_concepts"], 4)
            if counts["ontology_concepts"]
            else 0.0
        )
        candidate_definitions = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM ontology_concepts
                WHERE definition_status = 'candidate'
                """
            ).fetchone()[0]
        )

        linked_training_courses = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT training_course_id)
                FROM ncs_training_course_concept_links
                """
            ).fetchone()[0]
        )
        training_course_concept_coverage = (
            round(linked_training_courses / counts["ncs_training_courses"], 4)
            if counts["ncs_training_courses"]
            else 0.0
        )
        if counts["ncs_training_courses"] and linked_training_courses == 0:
            issues.append(
                {
                    "severity": "warning",
                    "check": "training_course_concept_links",
                    "detail": "No training courses are linked to KSA concepts.",
                }
            )
        element_linked_training_courses = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT training_course_id)
                FROM ncs_training_course_element_links
                """
            ).fetchone()[0]
        )
        goal_linked_training_courses = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT training_course_id)
                FROM training_goal_concept_links
                """
            ).fetchone()[0]
        )
        delivery_linked_training_courses = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT training_course_id)
                FROM training_delivery_relations
                """
            ).fetchone()[0]
        )
        training_course_element_coverage = (
            round(element_linked_training_courses / counts["ncs_training_courses"], 4)
            if counts["ncs_training_courses"]
            else 0.0
        )
        training_goal_concept_coverage = (
            round(goal_linked_training_courses / counts["ncs_training_courses"], 4)
            if counts["ncs_training_courses"]
            else 0.0
        )
        training_delivery_coverage = (
            round(delivery_linked_training_courses / counts["ncs_training_courses"], 4)
            if counts["ncs_training_courses"]
            else 0.0
        )
        if counts["ncs_training_courses"] and element_linked_training_courses == 0:
            issues.append(
                {
                    "severity": "warning",
                    "check": "training_course_element_links",
                    "detail": "No training courses are linked to competency elements.",
                }
            )
        if counts["ncs_training_courses"] and delivery_linked_training_courses == 0:
            issues.append(
                {
                    "severity": "warning",
                    "check": "training_delivery_relations",
                    "detail": "No training delivery relations are available.",
                }
            )

        saved_training_recommendations = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM education_recommendation_evidence
                WHERE source_table = 'ncs_training_courses'
                """
            ).fetchone()[0]
        )

        return {
            "ok": not any(issue["severity"] == "error" for issue in issues),
            "counts": counts,
            "metrics": {
                "unlinked_ksa_items": unlinked_ksa_items,
                "atomic_without_concepts": atomic_without_concepts,
                "tasks_with_similarity": tasks_with_similarity,
                "task_similarity_coverage": task_similarity_coverage,
                "concepts_with_ksa_meanings": concepts_with_meanings,
                "ksa_meaning_coverage": ksa_meaning_coverage,
                "candidate_definitions": candidate_definitions,
                "linked_training_courses": linked_training_courses,
                "training_course_concept_coverage": training_course_concept_coverage,
                "element_linked_training_courses": element_linked_training_courses,
                "training_course_element_coverage": training_course_element_coverage,
                "goal_linked_training_courses": goal_linked_training_courses,
                "training_goal_concept_coverage": training_goal_concept_coverage,
                "delivery_linked_training_courses": delivery_linked_training_courses,
                "training_delivery_coverage": training_delivery_coverage,
                "saved_training_recommendations": saved_training_recommendations,
            },
            "issues": issues,
            "note": "This validates NCS task/KSA/training-course recommendation readiness.",
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Export and validate the NCS ontology graph.")
    parser.add_argument("action", choices=["export-jsonld", "validate"])
    parser.add_argument("--db", type=Path, default=Path("data/processed/ncs.db"))
    parser.add_argument("--out", type=Path, default=Path("exports/ncs_training_ontology.jsonld"))
    parser.add_argument("--include-excluded-mappings", action="store_true")
    parser.add_argument("--no-chunk-evidence", action="store_true")
    parser.add_argument("--chunk-evidence-limit", type=int, default=50000)
    parser.add_argument("--no-document-chunks", action="store_true")
    parser.add_argument("--document-chunk-limit", type=int, default=20000)
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
            include_document_chunks=not args.no_document_chunks,
            document_chunk_limit=args.document_chunk_limit,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
