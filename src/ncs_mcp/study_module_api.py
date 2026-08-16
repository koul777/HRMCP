from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import unquote

import requests

from ncs_mcp.db import connect, create_indexes, initialize_database, normalize_spaces, now_utc


DEFAULT_STUDY_MODULE_API_URL = "https://apis.data.go.kr/B490007/ncsStudyModule/openapi21"


def _child_text_map(element: ET.Element) -> dict[str, str]:
    return {
        child.tag.split("}", 1)[-1]: (child.text or "").strip()
        for child in list(element)
    }


def parse_study_module_xml(xml_text: str) -> dict[str, Any]:
    root = ET.fromstring(xml_text)
    rows = [_child_text_map(row) for row in root.findall(".//row")]
    data_info = root.find(".//dataInfo")
    info = _child_text_map(data_info) if data_info is not None else {}
    return {
        "data_info": info,
        "items": rows,
        "item_count": len(rows),
    }


def fetch_study_modules(
    service_key: str,
    *,
    api_url: str = DEFAULT_STUDY_MODULE_API_URL,
    major_code: str | None = None,
    module_name: str | None = None,
    page_no: int = 1,
    num_of_rows: int = 10,
    timeout: int = 30,
) -> dict[str, Any]:
    normalized_key = unquote(service_key)
    params: dict[str, Any] = {
        "serviceKey": normalized_key,
        "pageNo": page_no,
        "numOfRows": num_of_rows,
        "returnType": "xml",
    }
    if major_code:
        params["ncsLclasCd"] = major_code
    if module_name:
        params["modulNm"] = module_name
    try:
        response = requests.get(api_url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        safe_params = {key: value for key, value in params.items() if key != "serviceKey"}
        raise RuntimeError(
            f"Study module API request failed: url={api_url}, params={safe_params}, "
            f"error={type(exc).__name__}"
        ) from None
    try:
        response.raise_for_status()
    except requests.HTTPError:
        safe_params = {key: value for key, value in params.items() if key != "serviceKey"}
        raise RuntimeError(
            f"Study module API request failed: url={api_url}, params={safe_params}, "
            f"status={response.status_code}"
        ) from None
    payload = parse_study_module_xml(response.text)
    payload["request"] = {
        "api_url": api_url,
        "major_code": major_code,
        "module_name": module_name,
        "page_no": page_no,
        "num_of_rows": num_of_rows,
    }
    return payload


def _as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _first_value(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _as_text(item.get(key))
        if value:
            return value
    return ""


def _as_int(value: Any, default: int = 0) -> int:
    text = _as_text(value).replace(",", "")
    return int(text) if text.isdigit() else default


def _module_seq(item: dict[str, Any]) -> str:
    return _first_value(item, "learnModulSeq", "learnModuleSeq", "learnModulNo", "learnModuleNo")


def _module_name(item: dict[str, Any]) -> str:
    return _first_value(item, "learnModulName", "learnModuleName", "modulNm", "moduleName")


def _module_text(item: dict[str, Any]) -> str:
    return _first_value(item, "learnModulText", "learnModuleText", "modulText", "moduleText")


def _module_record(item: dict[str, Any], fetched_at: str) -> dict[str, Any] | None:
    seq = _module_seq(item)
    name = _module_name(item)
    if not seq or not name:
        return None
    return {
        "learn_module_seq": seq,
        "learn_module_name": name,
        "learn_module_text": _module_text(item),
        "ncs_lclas_cd": _first_value(item, "ncsLclasCd", "NCS_LCLAS_CD"),
        "ncs_lclas_name": _first_value(item, "ncsLclasCdnm", "ncsLclasNm", "NCS_LCLAS_CDNM"),
        "ncs_mclas_cd": _first_value(item, "ncsMclasCd", "NCS_MCLAS_CD"),
        "ncs_mclas_name": _first_value(item, "ncsMclasCdnm", "ncsMclasNm", "NCS_MCLAS_CDNM"),
        "ncs_sclas_cd": _first_value(item, "ncsSclasCd", "NCS_SCLAS_CD"),
        "ncs_sclas_name": _first_value(item, "ncsSclasCdnm", "ncsSclasNm", "NCS_SCLAS_CDNM"),
        "ncs_subd_cd": _first_value(item, "ncsSubdCd", "NCS_SUBD_CD"),
        "ncs_subd_name": _first_value(item, "ncsSubdCdnm", "ncsSubdNm", "NCS_SUBD_CDNM"),
        "source_payload": json.dumps(item, ensure_ascii=False, sort_keys=True),
        "api_fetched_at": fetched_at,
    }


def upsert_study_modules(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> int:
    count = 0
    fetched_at = now_utc()
    for item in items:
        record = _module_record(item, fetched_at)
        if record is None:
            continue
        conn.execute(
            """
            INSERT INTO ncs_learning_modules(
                learn_module_seq, learn_module_name, learn_module_text,
                ncs_lclas_cd, ncs_lclas_name, ncs_mclas_cd, ncs_mclas_name,
                ncs_sclas_cd, ncs_sclas_name, ncs_subd_cd, ncs_subd_name,
                source_payload, api_fetched_at
            ) VALUES (
                :learn_module_seq, :learn_module_name, :learn_module_text,
                :ncs_lclas_cd, :ncs_lclas_name, :ncs_mclas_cd, :ncs_mclas_name,
                :ncs_sclas_cd, :ncs_sclas_name, :ncs_subd_cd, :ncs_subd_name,
                :source_payload, :api_fetched_at
            )
            ON CONFLICT(learn_module_seq) DO UPDATE SET
                learn_module_name = excluded.learn_module_name,
                learn_module_text = excluded.learn_module_text,
                ncs_lclas_cd = excluded.ncs_lclas_cd,
                ncs_lclas_name = excluded.ncs_lclas_name,
                ncs_mclas_cd = excluded.ncs_mclas_cd,
                ncs_mclas_name = excluded.ncs_mclas_name,
                ncs_sclas_cd = excluded.ncs_sclas_cd,
                ncs_sclas_name = excluded.ncs_sclas_name,
                ncs_subd_cd = excluded.ncs_subd_cd,
                ncs_subd_name = excluded.ncs_subd_name,
                source_payload = excluded.source_payload,
                api_fetched_at = excluded.api_fetched_at
            """,
            record,
        )
        count += 1
    return count


def _linked_unit_rows(conn: sqlite3.Connection, module_seq: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT DISTINCT cu.unit_code, cu.unit_name_raw AS unit_name
        FROM learning_module_unit_links link
        JOIN competency_units cu ON cu.unit_code = link.unit_code
        WHERE link.learn_module_seq = ?
          AND link.review_status != 'rejected'
        ORDER BY link.confidence_score DESC, cu.unit_code
        """,
        (module_seq,),
    ).fetchall()


def refresh_learning_module_links(
    conn: sqlite3.Connection,
    *,
    module_seqs: list[str] | None = None,
    max_units_per_module: int = 80,
    max_concepts_per_module: int = 50,
) -> dict[str, int]:
    clauses: list[str] = []
    params: list[Any] = []
    if module_seqs is not None:
        if not module_seqs:
            return {
                "modules_processed": 0,
                "unit_links_upserted": 0,
                "concept_links_upserted": 0,
            }
        clauses.append(f"learn_module_seq IN ({','.join('?' for _ in module_seqs)})")
        params.extend(module_seqs)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    modules = conn.execute(
        f"""
        SELECT *
        FROM ncs_learning_modules
        {where}
        ORDER BY ncs_lclas_cd, ncs_mclas_cd, ncs_sclas_cd, ncs_subd_cd, learn_module_seq
        """,
        params,
    ).fetchall()
    timestamp = now_utc()
    unit_links = 0
    concept_links = 0
    for module in modules:
        seq = module["learn_module_seq"]
        unit_rows: list[sqlite3.Row] = []
        if module["ncs_subd_cd"]:
            unit_rows = conn.execute(
                """
                SELECT cu.unit_code, cu.unit_name_raw AS unit_name
                FROM competency_units cu
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE c.major_code = ?
                  AND c.middle_code = ?
                  AND c.small_code = ?
                  AND c.sub_code = ?
                ORDER BY cu.unit_code
                LIMIT ?
                """,
                (
                    module["ncs_lclas_cd"],
                    module["ncs_mclas_cd"],
                    module["ncs_sclas_cd"],
                    module["ncs_subd_cd"],
                    max_units_per_module,
                ),
            ).fetchall()
        elif module["ncs_sclas_cd"]:
            unit_rows = conn.execute(
                """
                SELECT cu.unit_code, cu.unit_name_raw AS unit_name
                FROM competency_units cu
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE c.major_code = ?
                  AND c.middle_code = ?
                  AND c.small_code = ?
                ORDER BY cu.unit_code
                LIMIT ?
                """,
                (
                    module["ncs_lclas_cd"],
                    module["ncs_mclas_cd"],
                    module["ncs_sclas_cd"],
                    max_units_per_module,
                ),
            ).fetchall()

        for unit in unit_rows:
            conn.execute(
                """
                INSERT INTO learning_module_unit_links(
                    learn_module_seq, unit_code, link_method, confidence_score,
                    evidence_text, review_status, created_at, updated_at
                ) VALUES (?, ?, 'classification_code', 0.65, ?, 'auto_linked', ?, ?)
                ON CONFLICT(learn_module_seq, unit_code, link_method) DO UPDATE SET
                    confidence_score = excluded.confidence_score,
                    evidence_text = excluded.evidence_text,
                    updated_at = excluded.updated_at
                """,
                (seq, unit["unit_code"], "NCS classification codes match.", timestamp, timestamp),
            )
            unit_links += 1

        text = normalize_spaces(f"{module['learn_module_name']} {module['learn_module_text'] or ''}").lower()
        for unit in _linked_unit_rows(conn, seq):
            unit_name = normalize_spaces(unit["unit_name"]).lower()
            if len(unit_name) >= 2 and unit_name in text:
                conn.execute(
                    """
                    INSERT INTO learning_module_unit_links(
                        learn_module_seq, unit_code, link_method, confidence_score,
                        evidence_text, review_status, created_at, updated_at
                    ) VALUES (?, ?, 'module_text_unit_name', 0.85, ?, 'auto_linked', ?, ?)
                    ON CONFLICT(learn_module_seq, unit_code, link_method) DO UPDATE SET
                        confidence_score = excluded.confidence_score,
                        evidence_text = excluded.evidence_text,
                        updated_at = excluded.updated_at
                    """,
                    (seq, unit["unit_code"], f"Module text mentions unit name: {unit['unit_name']}", timestamp, timestamp),
                )
                unit_links += 1

        concept_rows = conn.execute(
            """
            SELECT DISTINCT oc.concept_id, oc.concept_name
            FROM learning_module_unit_links link
            JOIN competency_elements ce ON ce.unit_code = link.unit_code
            JOIN ksa_items ki ON ki.element_id = ce.element_id
            JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
            JOIN ontology_concepts oc ON oc.concept_id = kcl.concept_id
            WHERE link.learn_module_seq = ?
              AND link.review_status != 'rejected'
            ORDER BY oc.concept_id
            LIMIT 1000
            """,
            (seq,),
        ).fetchall()
        used = 0
        for concept in concept_rows:
            concept_name = normalize_spaces(concept["concept_name"]).lower()
            if len(concept_name) < 2 or concept_name not in text:
                continue
            conn.execute(
                """
                INSERT INTO learning_module_concept_links(
                    learn_module_seq, concept_id, link_method, confidence_score,
                    evidence_text, review_status, created_at, updated_at
                ) VALUES (?, ?, 'module_text_concept_name', 0.8, ?, 'auto_linked', ?, ?)
                ON CONFLICT(learn_module_seq, concept_id, link_method) DO UPDATE SET
                    confidence_score = excluded.confidence_score,
                    evidence_text = excluded.evidence_text,
                    updated_at = excluded.updated_at
                """,
                (
                    seq,
                    concept["concept_id"],
                    f"Module text mentions concept: {concept['concept_name']}",
                    timestamp,
                    timestamp,
                ),
            )
            concept_links += 1
            used += 1
            if used >= max_concepts_per_module:
                break
    return {
        "modules_processed": len(modules),
        "unit_links_upserted": unit_links,
        "concept_links_upserted": concept_links,
    }


def collect_study_modules(
    db_path: Path | str,
    service_key: str,
    *,
    api_url: str = DEFAULT_STUDY_MODULE_API_URL,
    major_code: str | None = None,
    module_name: str | None = None,
    page_no: int = 1,
    num_of_rows: int = 200,
    timeout: int = 30,
    max_pages: int | None = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    collected_items: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    current_page = page_no
    try:
        while True:
            payload = fetch_study_modules(
                service_key,
                api_url=api_url,
                major_code=major_code,
                module_name=module_name,
                page_no=current_page,
                num_of_rows=num_of_rows,
                timeout=timeout,
            )
            items = payload["items"]
            collected_items.extend(items)
            info = payload.get("data_info") or {}
            total_count = _as_int(info.get("totalCount") or info.get("totCnt"))
            total_pages = _as_int(info.get("totalPage"), default=0)
            if not total_pages and total_count and num_of_rows:
                total_pages = (total_count + num_of_rows - 1) // num_of_rows
            pages.append(
                {
                    "page_no": current_page,
                    "item_count": len(items),
                    "total_count": total_count,
                    "total_pages": total_pages,
                }
            )
            if not items:
                break
            if max_pages is not None and len(pages) >= max_pages:
                break
            if total_pages and current_page >= total_pages:
                break
            if not total_pages:
                break
            current_page += 1

        upserted = upsert_study_modules(conn, collected_items)
        module_seqs = [_module_seq(item) for item in collected_items if _module_seq(item)]
        link_summary = refresh_learning_module_links(conn, module_seqs=module_seqs)
        create_indexes(conn)
        conn.commit()
        total_saved = int(conn.execute("SELECT COUNT(*) FROM ncs_learning_modules").fetchone()[0])
    finally:
        conn.close()
    return {
        "major_code": major_code,
        "module_name": module_name,
        "pages": pages,
        "items_received": len(collected_items),
        "modules_upserted": upserted,
        "modules_total": total_saved,
        "links": link_summary,
        "request": {
            "api_url": api_url,
            "page_no": page_no,
            "num_of_rows": num_of_rows,
            "max_pages": max_pages,
        },
    }
