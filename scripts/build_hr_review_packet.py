"""Build HR-scope transferability Human Review artifacts.

The script is report-only. It reads NCS DB evidence and OCR text, then writes
JSON/Markdown/CSV/HTML review packets without mutating the database.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import json
import re
import sqlite3
import argparse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATE_STAMP = "20260619"


def repo_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else ROOT / candidate


def artifact_paths_for_date(
    date_stamp: str = DEFAULT_DATE_STAMP,
    *,
    reports_dir: str | Path | None = None,
) -> dict[str, Path]:
    stamp = str(date_stamp or "").strip()
    if not re.fullmatch(r"\d{8}", stamp):
        raise ValueError("date_stamp must be YYYYMMDD")
    reports = repo_path(reports_dir or "reports")
    return {
        "main_json": reports / f"aihr_hr_job_movement_learning_path_review_{stamp}.json",
        "main_md": reports / f"aihr_hr_job_movement_learning_path_review_{stamp}.md",
        "manifest_json": reports / f"aihr_hr_learning_module_ocr_manifest_{stamp}.json",
        "cards_json": reports / f"aihr_hr_learning_path_ocr_context_cards_{stamp}.json",
        "cards_md": reports / f"aihr_hr_learning_path_ocr_context_cards_{stamp}.md",
        "packet_json": reports / f"aihr_hr_transferability_human_review_packet_{stamp}.json",
        "packet_md": reports / f"aihr_hr_transferability_human_review_packet_{stamp}.md",
        "packet_csv": reports / f"aihr_hr_transferability_human_review_packet_{stamp}.csv",
        "packet_html": reports / f"aihr_hr_transferability_human_review_packet_{stamp}.html",
        "matrix_csv": reports / f"aihr_hr_transferability_matrix_{stamp}.csv",
    }


def configure_artifact_paths(
    *,
    date_stamp: str = DEFAULT_DATE_STAMP,
    reports_dir: str | Path | None = None,
    db_path: str | Path | None = None,
    main_json: str | Path | None = None,
    main_md: str | Path | None = None,
    manifest_json: str | Path | None = None,
    cards_json: str | Path | None = None,
    cards_md: str | Path | None = None,
    packet_json: str | Path | None = None,
    packet_md: str | Path | None = None,
    packet_csv: str | Path | None = None,
    packet_html: str | Path | None = None,
    matrix_csv: str | Path | None = None,
) -> None:
    paths = artifact_paths_for_date(date_stamp, reports_dir=reports_dir)
    global DB_PATH, MAIN_JSON, MAIN_MD, MANIFEST_JSON, CARDS_JSON, CARDS_MD
    global PACKET_JSON, PACKET_MD, PACKET_CSV, PACKET_HTML, MATRIX_CSV
    DB_PATH = repo_path(db_path or "data/processed/ncs.db")
    MAIN_JSON = repo_path(main_json) if main_json is not None else paths["main_json"]
    MAIN_MD = repo_path(main_md) if main_md is not None else paths["main_md"]
    MANIFEST_JSON = repo_path(manifest_json) if manifest_json is not None else paths["manifest_json"]
    CARDS_JSON = repo_path(cards_json) if cards_json is not None else paths["cards_json"]
    CARDS_MD = repo_path(cards_md) if cards_md is not None else paths["cards_md"]
    PACKET_JSON = repo_path(packet_json) if packet_json is not None else paths["packet_json"]
    PACKET_MD = repo_path(packet_md) if packet_md is not None else paths["packet_md"]
    PACKET_CSV = repo_path(packet_csv) if packet_csv is not None else paths["packet_csv"]
    PACKET_HTML = repo_path(packet_html) if packet_html is not None else paths["packet_html"]
    MATRIX_CSV = repo_path(matrix_csv) if matrix_csv is not None else paths["matrix_csv"]


configure_artifact_paths()

NOW = dt.datetime.now(dt.timezone.utc).isoformat()
REVIEW_ONLY_FALSE_FIELDS = {
    "approval_claim": False,
    "db_writes": False,
    "active_scoring_source": False,
    "status_update_allowed": False,
}
REVIEW_ONLY_TRUE_FIELDS = {
    "review_only": True,
    "non_scoring": True,
}
HR_SCOPE = {
    "major_code": "02",
    "small_code": "020202",
    "job_code": "02020201",
    "job_name": "인사",
}

PAGE_RE = re.compile(r"===== PAGE (\d+) =====")
DOMAIN_TERMS = [
    "인사",
    "직무",
    "인력",
    "이동",
    "배치",
    "승진",
    "승격",
    "경력",
    "경력개발",
    "전직",
    "채용",
    "평가",
    "성과",
    "교육",
    "훈련",
    "임금",
    "급여",
    "퇴직",
    "복리",
    "후생",
    "조직문화",
    "아웃소싱",
    "근태",
    "소득세",
    "근로기준법",
    "개인정보",
    "역량",
    "직급",
    "보상",
    "멘토링",
    "코칭",
    "노사",
    "분석",
    "계획",
    "관리",
    "제도",
]
SNIPPET_SPECS = {
    "module_goal": {"terms": ["학습모듈의목표", "학습목표", "목표"], "min_page": 11},
    "prerequisite_learning": {"terms": ["선수학습"], "min_page": 11},
    "content_structure": {"terms": ["학습모듈의내용체계", "내용체계"], "min_page": 11},
    "required_knowledge": {"terms": ["필요지식"], "min_page": 14},
    "performance_content": {"terms": ["수행내용"], "min_page": 14},
    "evaluation_basis": {"terms": ["평가준거", "평가방법", "평가시고려사항"], "min_page": 14},
    "movement_context": {"terms": ["이동", "배치", "승진", "전직", "경력개발"], "min_page": 13},
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def apply_review_only_contract(payload: dict[str, Any]) -> None:
    payload.update(REVIEW_ONLY_FALSE_FIELDS)
    payload.update(REVIEW_ONLY_TRUE_FIELDS)


def review_only_contract_findings(payload: dict[str, Any], label: str) -> list[str]:
    findings: list[str] = []
    for field, expected in REVIEW_ONLY_FALSE_FIELDS.items():
        if payload.get(field) is not expected:
            findings.append(f"{label}.{field} must be {expected!r}")
    for field, expected in REVIEW_ONLY_TRUE_FIELDS.items():
        if payload.get(field) is not expected:
            findings.append(f"{label}.{field} must be {expected!r}")
    return findings


def require_review_only_contract(payload: dict[str, Any], label: str) -> None:
    findings = review_only_contract_findings(payload, label)
    if findings:
        raise ValueError("; ".join(findings))


def require_review_only_rows(rows: list[dict[str, Any]], label: str) -> None:
    findings: list[str] = []
    for index, row in enumerate(rows, start=1):
        findings.extend(review_only_contract_findings(row, f"{label}[{index}]"))
    if findings:
        raise ValueError("; ".join(findings))


def split_pages(text: str) -> list[dict[str, Any]]:
    matches = list(PAGE_RE.finditer(text))
    pages: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pages.append({"page": int(match.group(1)), "text": text[start:end]})
    if not pages and text:
        pages.append({"page": None, "text": text})
    return pages


def compact_text(value: str) -> str:
    value = value.replace("|", " ")
    return re.sub(r"\s+", " ", value).strip()


def condensed_with_map(value: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    indexes: list[int] = []
    for index, char in enumerate(value):
        if char.isspace():
            continue
        chars.append(char)
        indexes.append(index)
    return "".join(chars), indexes


def find_snippet(
    pages: list[dict[str, Any]],
    terms: list[str],
    *,
    min_page: int,
    max_chars: int = 360,
) -> dict[str, Any]:
    for page in pages:
        page_no = int(page.get("page") or 0)
        if page_no < min_page:
            continue
        raw = str(page.get("text") or "")
        condensed, index_map = condensed_with_map(raw)
        if not condensed:
            continue
        for term in terms:
            needle = re.sub(r"\s+", "", term)
            position = condensed.find(needle)
            if position < 0:
                continue
            original = index_map[position] if position < len(index_map) else 0
            start = max(0, original - 80)
            end = min(len(raw), original + max_chars)
            return {
                "found": True,
                "term": term,
                "page": page_no,
                "snippet": compact_text(raw[start:end]),
            }
    return {"found": False, "term": terms[0] if terms else None, "page": None, "snippet": ""}


def module_name_from_path(path: str) -> str:
    parts = Path(path).name.split("_")
    return parts[1] if len(parts) >= 2 else Path(path).stem


def build_ocr_cards() -> dict[str, Any]:
    manifest = read_json(MANIFEST_JSON)
    cards: list[dict[str, Any]] = []
    for record in manifest.get("records", []):
        text_path = ROOT / record["txt_path"]
        text = text_path.read_text(encoding="utf-8", errors="replace")
        pages = split_pages(text)
        condensed, _ = condensed_with_map(text)
        top_terms = [
            {"term": term, "count": condensed.count(re.sub(r"\s+", "", term))}
            for term in DOMAIN_TERMS
        ]
        top_terms = [item for item in sorted(top_terms, key=lambda x: (-x["count"], x["term"])) if item["count"] > 0][:12]
        snippets = {
            key: find_snippet(pages, spec["terms"], min_page=int(spec["min_page"]))
            for key, spec in SNIPPET_SPECS.items()
        }
        chars = len(text)
        hangul = sum(1 for char in text if "\uac00" <= char <= "\ud7a3")
        hangul_ratio = round(hangul / max(chars, 1), 4)
        cards.append(
            {
                "unit_base_code": Path(record["pdf_path"]).name[:10],
                "module_name": module_name_from_path(record["pdf_path"]),
                "pdf_path": record["pdf_path"],
                "text_path": record["txt_path"],
                "page_count": record.get("page_count"),
                "chars": chars,
                "hangul_chars": hangul,
                "hangul_ratio": hangul_ratio,
                "status": "ocr_context_ready" if hangul_ratio >= 0.25 else "ocr_quality_review_required",
                "intro_pages_filtered": True,
                "top_terms": top_terms,
                "snippets": snippets,
                "review_use": "auxiliary_human_review_reference_not_scoring_source",
                "approval_claim": False,
                "db_writes": False,
                "active_scoring_source": False,
                "review_only": True,
                "non_scoring": True,
                "status_update_allowed": False,
                "score_usage": "human_review_context_only_not_recommendation_scoring",
            }
        )

    payload = {
        "schema": "aihr_hr_learning_module_ocr_context_cards_v2",
        "created_at": NOW,
        "scope": HR_SCOPE,
        "status": "review_required",
        "contract_ok": True,
        "approval_ready": False,
        "approval_claim": False,
        "db_writes": False,
        "active_scoring_source": False,
        "review_only": True,
        "non_scoring": True,
        "status_update_allowed": False,
        "manifest": str(MANIFEST_JSON.relative_to(ROOT)),
        "summary": {
            "card_count": len(cards),
            "ocr_context_ready_count": sum(1 for card in cards if card["status"] == "ocr_context_ready"),
            "ocr_quality_review_required_count": sum(1 for card in cards if card["status"] != "ocr_context_ready"),
            "total_pages": sum(int(card.get("page_count") or 0) for card in cards),
            "total_chars": sum(int(card.get("chars") or 0) for card in cards),
            "total_hangul_chars": sum(int(card.get("hangul_chars") or 0) for card in cards),
            "intro_pages_filtered": True,
            "approval_claim": False,
            "db_writes": False,
            "active_scoring_source": False,
            "review_only": True,
            "non_scoring": True,
            "status_update_allowed": False,
        },
        "review_policy": {
            "approval_claim": False,
            "db_writes": False,
            "active_scoring_source": False,
            "review_only": True,
            "non_scoring": True,
            "status_update_allowed": False,
            "human_decision_required": True,
        },
        "card_count": len(cards),
        "cards": cards,
    }
    require_review_only_contract(payload, "ocr_context_cards")
    write_json(CARDS_JSON, payload)
    write_ocr_cards_md(payload)
    return payload


def write_ocr_cards_md(payload: dict[str, Any]) -> None:
    lines = [
        "# HR Learning Module OCR Context Cards",
        "",
        "- Schema: `aihr_hr_learning_module_ocr_context_cards_v2`",
        "- Scope: 인사 `02020201`",
        f"- Cards: {payload['card_count']}",
        f"- OCR context ready: {payload['summary']['ocr_context_ready_count']}",
        "- Use: auxiliary Human Review reference only; not an active scoring or approval source.",
        "- Intro pages filtered: true",
        "",
        "| unit | module | pages | hangul ratio | top terms | goal page | movement page | status |",
        "|---|---|---:|---:|---|---:|---:|---|",
    ]
    for card in payload["cards"]:
        goal = card["snippets"]["module_goal"]
        movement = card["snippets"]["movement_context"]
        terms = ", ".join(f"{item['term']}:{item['count']}" for item in card["top_terms"][:5])
        lines.append(
            f"| {card['unit_base_code']} | {card['module_name']} | {card.get('page_count')} | "
            f"{card['hangul_ratio']} | {terms} | {goal.get('page') or ''} | "
            f"{movement.get('page') or ''} | {card['status']} |"
        )
    lines.extend(["", "## Module Evidence Snippets", ""])
    for card in payload["cards"]:
        lines.append(f"### {card['unit_base_code']} {card['module_name']}")
        for key in ["module_goal", "required_knowledge", "performance_content", "evaluation_basis", "movement_context"]:
            snippet = str(card["snippets"][key].get("snippet") or "")
            if len(snippet) > 260:
                snippet = snippet[:260].rstrip() + "..."
            lines.append(f"- {key}: page {card['snippets'][key].get('page') or '-'} / {snippet}")
        lines.append("")
    CARDS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def update_main_artifact(cards_payload: dict[str, Any]) -> dict[str, Any]:
    main = read_json(MAIN_JSON)
    apply_review_only_contract(main)
    main["source_policy"]["learning_modules"] = (
        "auxiliary_review_reference; OCR text available for HR/인사 modules; not active scoring source"
    )
    main["summary"]["ocr_or_metadata_only_units"] = 0
    main["summary"]["ocr_context_ready_units"] = cards_payload["summary"]["ocr_context_ready_count"]
    main["summary"]["ocr_quality_review_required_units"] = cards_payload["summary"][
        "ocr_quality_review_required_count"
    ]
    main["summary"].update(REVIEW_ONLY_FALSE_FIELDS)
    main["summary"].update(REVIEW_ONLY_TRUE_FIELDS)
    main["ocr_context_cards"] = str(CARDS_JSON.relative_to(ROOT))
    for item in main.get("correction_queue", []):
        if isinstance(item, dict):
            item.update(REVIEW_ONLY_FALSE_FIELDS)
            item.update(REVIEW_ONLY_TRUE_FIELDS)
            item["score_usage"] = "human_review_prioritization_only_not_recommendation_scoring"
    main["findings"] = [
        finding
        for finding in main.get("findings", [])
        if finding.get("code") != "hr_learning_modules_ocr_required"
    ]
    if not any(finding.get("code") == "hr_learning_modules_ocr_context_ready" for finding in main["findings"]):
        main["findings"].insert(
            0,
            {
                "severity": "review",
                "code": "hr_learning_modules_ocr_context_ready",
                "message": (
                    "인사 직무 학습모듈 15개 PDF의 OCR 본문과 주요 근거 카드가 준비됐지만, "
                    "활성 추천 점수나 승인 근거가 아니라 Human Review 보조 자료로만 사용한다."
                ),
                "count": cards_payload["summary"]["ocr_context_ready_count"],
            },
        )
    require_review_only_contract(main, "main_review_artifact")
    write_json(MAIN_JSON, main)
    write_main_md(main)
    return main


def write_main_md(main: dict[str, Any]) -> None:
    lines = [
        "# HR Job Movement Learning Path Review",
        "",
        "- Schema: `aihr_hr_job_movement_learning_path_review_v1`",
        "- Scope: 인사 `02020201`",
        f"- Status: `{main.get('status')}`",
        f"- Approval ready: `{main.get('approval_ready')}`",
        f"- Approval claim: `{main.get('approval_claim')}`",
        f"- DB writes: `{main.get('db_writes')}`",
        f"- Active scoring source: `{main.get('active_scoring_source')}`",
        f"- Review only: `{main.get('review_only')}`",
        f"- Non-scoring: `{main.get('non_scoring')}`",
        f"- Status update allowed: `{main.get('status_update_allowed')}`",
        "- Learning modules: auxiliary Human Review reference only; OCR context ready.",
        "",
        "## Summary",
        "",
    ]
    for key, value in main.get("summary", {}).items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Report Basis", "", "| page | basis |", "|---:|---|"])
    for basis in main.get("report_basis", []):
        lines.append(f"| {basis.get('page')} | {basis.get('basis')} |")
    lines.extend(
        [
            "",
            "## Learning Path",
            "",
            "| unit | name | level | career | KSA concepts | module | OCR status | top OCR terms |",
            "|---|---|---:|---|---:|---:|---|---|",
        ]
    )
    for row in main.get("learning_path", []):
        terms = ", ".join(f"{item['term']}:{item['count']}" for item in row.get("ocr_top_terms", [])[:5])
        lines.append(
            f"| {row.get('unit_code')} | {row.get('unit_name')} | {row.get('level')} | "
            f"{row.get('career_position')} | {row.get('ksa_concept_count')} | "
            f"{row.get('module_file_count')} | {row.get('module_extraction_status')} | {terms} |"
        )
    lines.extend(
        [
            "",
            "## Top Transferability Pairs",
            "",
            "| source | target | move | exact KSA | task max | report-grounded | priority |",
            "|---|---|---|---:|---:|---:|---|",
        ]
    )
    for pair in main.get("top_transferability_pairs", [])[:20]:
        lines.append(
            f"| {pair.get('source_unit_name')} | {pair.get('target_unit_name')} | "
            f"{pair.get('movement_type')} | {pair.get('exact_ksa_overlap_ratio')} | "
            f"{pair.get('task_similarity_max')} | {pair.get('report_grounded_transferability_ratio')} | "
            f"{pair.get('review_priority')} |"
        )
    lines.extend(["", "## Correction Queue", "", "| target | type | priority | reason | db writes |", "|---|---|---:|---|---|"])
    for item in main.get("correction_queue", []):
        lines.append(
            f"| {item.get('target')} | {item.get('type')} | {item.get('priority_score')} | "
            f"{item.get('reason')} | {item.get('db_writes_allowed')} |"
        )
    lines.extend(["", "## Findings", ""])
    for finding in main.get("findings", []):
        lines.append(f"- {finding.get('severity')}:{finding.get('code')} {finding.get('message')}")
    MAIN_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_packet(cards_payload: dict[str, Any], main: dict[str, Any]) -> dict[str, Any]:
    conn = connect_db()
    try:
        units = rows(
            conn,
            """
            SELECT unit_code, base_unit_code, unit_name_raw, unit_level_raw, api_definition, review_status
            FROM competency_units
            WHERE base_unit_code LIKE '02020201%'
            ORDER BY CAST(COALESCE(unit_level_raw, '0') AS INTEGER), base_unit_code
            """,
        )
        unit_codes = [unit["unit_code"] for unit in units]
        unit_by_code = {unit["unit_code"]: unit for unit in units}
        career_by_unit = load_career_rows(conn, unit_codes)
        concepts_by_unit = load_concepts(conn)
        criteria_by_unit = load_criteria_samples(conn)
        ksa_counts, ksa_samples = load_ksa_context(conn)
        cards_by_base = {card["unit_base_code"]: card for card in cards_payload["cards"]}

        pairs = build_pairs(conn, unit_codes, unit_by_code, career_by_unit, concepts_by_unit, cards_by_base)
        selected_pairs = select_pairs(pairs, limit=12)
        review_units = build_review_units(
            units,
            unit_by_code,
            career_by_unit,
            concepts_by_unit,
            criteria_by_unit,
            ksa_counts,
            ksa_samples,
            cards_by_base,
        )

        packet = {
            "schema": "aihr_hr_transferability_human_review_packet_v1",
            "created_at": NOW,
            "scope": HR_SCOPE,
            "status": "review_required",
            "contract_ok": True,
            "approval_ready": False,
            "approval_claim": False,
            "db_writes": False,
            "active_scoring_source": False,
            "review_only": True,
            "non_scoring": True,
            "status_update_allowed": False,
            "human_decision_required": True,
            "source_policy": {
                "ncs_report": "movement and education-system methodology basis",
                "ncs_db": "structured NCS unit/element/criteria/KSA ontology evidence",
                "learning_modules": "OCR auxiliary Human Review evidence; not active scoring or approval source",
                "career_paths": "horizontal/vertical movement context; DB review status is not treated as a new human decision here",
            },
            "report_movement_model": report_movement_model(main),
            "summary": {
                "unit_count": len(units),
                "directed_pair_count": len(pairs),
                "selected_review_pair_count": len(selected_pairs),
                "medium_or_high_pair_count": sum(
                    1 for pair in pairs if pair["review_priority"] in {"high_review_candidate", "medium_review_candidate"}
                ),
                "high_pair_count": sum(1 for pair in pairs if pair["review_priority"] == "high_review_candidate"),
                "medium_pair_count": sum(1 for pair in pairs if pair["review_priority"] == "medium_review_candidate"),
                "learning_module_card_count": len(cards_payload["cards"]),
                "ocr_context_ready_count": cards_payload["summary"]["ocr_context_ready_count"],
                "approval_claim": False,
                "db_writes": False,
                "active_scoring_source": False,
                "review_only": True,
                "non_scoring": True,
                "status_update_allowed": False,
            },
            "review_policy": {
                "approval_claim": False,
                "db_writes": False,
                "active_scoring_source": False,
                "review_only": True,
                "non_scoring": True,
                "status_update_allowed": False,
                "human_decision_required": True,
            },
            "review_units": review_units,
            "selected_review_pairs": selected_pairs,
            "all_pair_artifact": str(MATRIX_CSV.relative_to(ROOT)),
            "review_questions": review_questions(),
        }
        require_review_only_contract(packet, "transferability_packet")
        require_review_only_rows(review_units, "review_units")
        require_review_only_rows(selected_pairs, "selected_review_pairs")
        write_json(PACKET_JSON, packet)
        write_matrix_csv(pairs)
        write_packet_csv(selected_pairs)
        write_packet_md(packet)
        write_packet_html(packet)
        return packet
    finally:
        conn.close()


def load_career_rows(conn: sqlite3.Connection, unit_codes: list[str]) -> dict[str, dict[str, Any]]:
    if not unit_codes:
        return {}
    placeholders = ",".join("?" for _ in unit_codes)
    result: dict[str, dict[str, Any]] = {}
    for row in rows(
        conn,
        f"""
        SELECT matched_unit_code, competency_name, competency_level_raw, position_name, position_level_raw, review_status
        FROM ncs_career_paths
        WHERE matched_unit_code IN ({placeholders})
        ORDER BY CAST(COALESCE(position_level_raw,'0') AS INTEGER), competency_name
        """,
        tuple(unit_codes),
    ):
        result.setdefault(row["matched_unit_code"], row)
    return result


def load_concepts(conn: sqlite3.Connection) -> defaultdict[str, dict[int, dict[str, Any]]]:
    concepts: defaultdict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in rows(
        conn,
        """
        SELECT cu.unit_code, cu.base_unit_code, oc.concept_id, oc.concept_name, oc.concept_type,
               COUNT(DISTINCT ki.ksa_id) AS ksa_evidence_count
        FROM competency_units cu
        JOIN competency_elements ce ON ce.unit_code = cu.unit_code
        JOIN ksa_items ki ON ki.element_id = ce.element_id
        JOIN ksa_concept_links link ON link.ksa_id = ki.ksa_id
        JOIN ontology_concepts oc ON oc.concept_id = link.concept_id
        WHERE cu.base_unit_code LIKE '02020201%'
        GROUP BY cu.unit_code, cu.base_unit_code, oc.concept_id, oc.concept_name, oc.concept_type
        ORDER BY cu.unit_code, oc.concept_type, oc.concept_name
        """,
    ):
        concepts[row["unit_code"]][row["concept_id"]] = row
    return concepts


def load_criteria_samples(conn: sqlite3.Connection) -> defaultdict[str, list[dict[str, Any]]]:
    samples: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows(
        conn,
        """
        SELECT cu.unit_code, ce.element_name_raw, pc.criteria_no, pc.criteria_text_raw
        FROM competency_units cu
        JOIN competency_elements ce ON ce.unit_code = cu.unit_code
        JOIN performance_criteria pc ON pc.element_id = ce.element_id
        WHERE cu.base_unit_code LIKE '02020201%'
        ORDER BY cu.unit_code, ce.element_no, pc.criteria_no
        """,
    ):
        if len(samples[row["unit_code"]]) < 6:
            samples[row["unit_code"]].append(row)
    return samples


def load_ksa_context(conn: sqlite3.Connection) -> tuple[defaultdict[str, Counter[str]], defaultdict[str, list[dict[str, Any]]]]:
    counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    samples: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows(
        conn,
        """
        SELECT cu.unit_code, ki.ksa_type_name, ki.ksa_text_raw
        FROM competency_units cu
        JOIN competency_elements ce ON ce.unit_code = cu.unit_code
        JOIN ksa_items ki ON ki.element_id = ce.element_id
        WHERE cu.base_unit_code LIKE '02020201%'
        ORDER BY cu.unit_code, ki.ksa_type_name, ki.ksa_no
        """,
    ):
        counts[row["unit_code"]][row["ksa_type_name"]] += 1
        if len(samples[row["unit_code"]]) < 8:
            samples[row["unit_code"]].append(row)
    return counts, samples


def concept_type_summary(concepts: list[dict[str, Any]]) -> dict[str, int]:
    counter = Counter(str(row.get("concept_type") or "unknown") for row in concepts)
    return dict(sorted(counter.items()))


def concept_brief(concepts: list[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    return [
        {
            "concept_id": row["concept_id"],
            "concept_name": row["concept_name"],
            "concept_type": row["concept_type"],
            "ksa_evidence_count": row["ksa_evidence_count"],
        }
        for row in concepts[:limit]
    ]


def level_of(unit_code: str, unit_by_code: dict[str, dict[str, Any]]) -> int:
    try:
        return int((unit_by_code.get(unit_code) or {}).get("unit_level_raw") or 0)
    except ValueError:
        return 0


def unit_name(unit_code: str, unit_by_code: dict[str, dict[str, Any]]) -> str:
    return str((unit_by_code.get(unit_code) or {}).get("unit_name_raw") or unit_code)


def movement_type(source: str, target: str, unit_by_code: dict[str, dict[str, Any]]) -> str:
    delta = level_of(target, unit_by_code) - level_of(source, unit_by_code)
    if delta == 0:
        return "horizontal_same_level"
    if delta > 0:
        return "vertical_up"
    return "vertical_down_or_lateral_support"


def report_component(source: str, target: str, unit_by_code: dict[str, dict[str, Any]]) -> float:
    delta = level_of(target, unit_by_code) - level_of(source, unit_by_code)
    if delta == 0:
        return 0.16
    if delta > 0:
        return max(0.04, 0.14 - 0.03 * (delta - 1))
    return max(0.03, 0.10 - 0.02 * (abs(delta) - 1))


def task_summary(conn: sqlite3.Connection, source: str, target: str) -> tuple[float, int]:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(similarity_score),0) AS max_score, COUNT(*) AS link_count
        FROM task_similarity_links
        WHERE source_unit_code = ? AND target_unit_code = ?
        """,
        (source, target),
    ).fetchone()
    return float(row["max_score"] or 0), int(row["link_count"] or 0)


def task_examples(conn: sqlite3.Connection, source: str, target: str) -> list[dict[str, Any]]:
    return rows(
        conn,
        """
        SELECT tsl.similarity_score, tsl.shared_concept_count,
               sce.element_name_raw AS source_element, spc.criteria_text_raw AS source_criteria,
               tce.element_name_raw AS target_element, tpc.criteria_text_raw AS target_criteria
        FROM task_similarity_links tsl
        JOIN performance_criteria spc ON spc.criteria_id = tsl.source_criteria_id
        JOIN competency_elements sce ON sce.element_id = spc.element_id
        JOIN performance_criteria tpc ON tpc.criteria_id = tsl.target_criteria_id
        JOIN competency_elements tce ON tce.element_id = tpc.element_id
        WHERE tsl.source_unit_code = ? AND tsl.target_unit_code = ?
        ORDER BY tsl.similarity_score DESC, tsl.shared_concept_count DESC
        LIMIT 4
        """,
        (source, target),
    )


def build_pairs(
    conn: sqlite3.Connection,
    unit_codes: list[str],
    unit_by_code: dict[str, dict[str, Any]],
    career_by_unit: dict[str, dict[str, Any]],
    concepts_by_unit: defaultdict[str, dict[int, dict[str, Any]]],
    cards_by_base: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for source in unit_codes:
        for target in unit_codes:
            if source == target:
                continue
            source_concepts = concepts_by_unit[source]
            target_concepts = concepts_by_unit[target]
            shared_ids = sorted(set(source_concepts) & set(target_concepts))
            target_only_ids = sorted(set(target_concepts) - set(source_concepts))
            shared_rows = [source_concepts[item] for item in shared_ids]
            target_only_rows = [target_concepts[item] for item in target_only_ids]
            exact_ratio = len(shared_ids) / max(len(target_concepts), 1)
            task_max, task_count = task_summary(conn, source, target)
            movement_component = report_component(source, target, unit_by_code)
            review_ratio = min(1.0, exact_ratio + task_max * 0.55 + movement_component)
            priority = "context_only"
            if review_ratio >= 0.45:
                priority = "high_review_candidate"
            elif review_ratio >= 0.30:
                priority = "medium_review_candidate"
            pairs.append(
                {
                    "source_unit_code": source,
                    "source_unit_name": unit_name(source, unit_by_code),
                    "source_level": level_of(source, unit_by_code),
                    "source_career_position": (career_by_unit.get(source) or {}).get("position_name"),
                    "target_unit_code": target,
                    "target_unit_name": unit_name(target, unit_by_code),
                    "target_level": level_of(target, unit_by_code),
                    "target_career_position": (career_by_unit.get(target) or {}).get("position_name"),
                    "movement_type": movement_type(source, target, unit_by_code),
                    "level_delta": level_of(target, unit_by_code) - level_of(source, unit_by_code),
                    "exact_ksa_overlap_ratio": round(exact_ratio, 4),
                    "shared_ksa_concept_count": len(shared_ids),
                    "target_ksa_concept_count": len(target_concepts),
                    "target_only_ksa_concept_count": len(target_only_ids),
                    "shared_concept_type_summary": concept_type_summary(shared_rows),
                    "target_only_concept_type_summary": concept_type_summary(target_only_rows),
                    "task_similarity_max": round(task_max, 4),
                    "task_similarity_link_count": task_count,
                    "report_movement_component": round(movement_component, 4),
                    "report_grounded_transferability_ratio": round(review_ratio, 4),
                    "score_usage": "human_review_prioritization_only_not_recommendation_scoring",
                    "review_priority": priority,
                    "approval_claim": False,
                    "db_writes": False,
                    "active_scoring_source": False,
                    "review_only": True,
                    "non_scoring": True,
                    "status_update_allowed": False,
                    "shared_ksa_concepts": concept_brief(shared_rows),
                    "target_only_gap_concepts": concept_brief(target_only_rows),
                    "task_similarity_examples": task_examples(conn, source, target),
                    "source_learning_module_context": cards_by_base.get(
                        str((unit_by_code.get(source) or {}).get("base_unit_code") or "")
                    ),
                    "target_learning_module_context": cards_by_base.get(
                        str((unit_by_code.get(target) or {}).get("base_unit_code") or "")
                    ),
                    "human_review_template": human_review_template(),
                }
            )
    pairs.sort(
        key=lambda pair: (
            -float(pair["report_grounded_transferability_ratio"]),
            str(pair["source_unit_name"]),
            str(pair["target_unit_name"]),
        )
    )
    return pairs


def human_review_template() -> dict[str, Any]:
    return {
        "decision_status": "pending_human_review",
        "reviewer": "",
        "decision": "",
        "rationale": "",
        "ontology_correction_action": "",
        "suggested_actions": [
            "accept_transferability_candidate_after_human_evidence_check",
            "lower_transferability_due_to_generic_or_weak_shared_ksa",
            "add_or_refine_task_ksa_relation_candidate",
            "request_additional_training_evidence",
            "reject_as_non_transferable_for_current_context",
        ],
        "db_writes_allowed": False,
        "approval_claim": False,
        "db_writes": False,
        "active_scoring_source": False,
        "review_only": True,
        "non_scoring": True,
        "status_update_allowed": False,
    }


def select_pairs(pairs: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    selected = [
        pair
        for pair in pairs
        if pair["review_priority"] in {"high_review_candidate", "medium_review_candidate"}
    ]
    seen = {(pair["source_unit_code"], pair["target_unit_code"]) for pair in selected}
    for pair in pairs:
        key = (pair["source_unit_code"], pair["target_unit_code"])
        if key not in seen:
            selected.append(pair)
            seen.add(key)
        if len(selected) >= limit:
            break
    return selected[:limit]


def build_review_units(
    units: list[dict[str, Any]],
    unit_by_code: dict[str, dict[str, Any]],
    career_by_unit: dict[str, dict[str, Any]],
    concepts_by_unit: defaultdict[str, dict[int, dict[str, Any]]],
    criteria_by_unit: defaultdict[str, list[dict[str, Any]]],
    ksa_counts: defaultdict[str, Counter[str]],
    ksa_samples: defaultdict[str, list[dict[str, Any]]],
    cards_by_base: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    review_units = []
    for unit in units:
        unit_code = unit["unit_code"]
        concepts = list(concepts_by_unit[unit_code].values())
        review_units.append(
            {
                "unit_code": unit_code,
                "base_unit_code": unit["base_unit_code"],
                "unit_name": unit["unit_name_raw"],
                "level": level_of(unit_code, unit_by_code),
                "definition": unit.get("api_definition"),
                "career_path": career_by_unit.get(unit_code),
                "concept_count": len(concepts),
                "concept_type_summary": concept_type_summary(concepts),
                "ksa_type_counts": dict(ksa_counts[unit_code]),
                "performance_criteria_samples": criteria_by_unit[unit_code],
                "ksa_samples": ksa_samples[unit_code],
                "learning_module_context": cards_by_base.get(unit["base_unit_code"]),
                "review_state": "candidate_needs_human_review",
                "approval_claim": False,
                "db_writes": False,
                "active_scoring_source": False,
                "review_only": True,
                "non_scoring": True,
                "status_update_allowed": False,
            }
        )
    return review_units


def report_movement_model(main: dict[str, Any]) -> dict[str, Any]:
    return {
        "basis": main.get("report_basis", []),
        "movement_components": [
            {"component": "task_ksa_chain", "meaning": "직무명 문자열이 아니라 능력단위-요소-수행준거-KSA 사슬로 비교"},
            {"component": "horizontal_same_level", "meaning": "같은 수준 내 수평 이동 후보"},
            {"component": "vertical_up", "meaning": "상위 수준 이동 후보이며 수준/권한/경력 차이 확인 필요"},
            {"component": "vertical_down_or_lateral_support", "meaning": "하위 또는 보조 직무 이동 후보이며 과잉 추천 여부 확인 필요"},
            {"component": "self_diagnosis_gap_missing", "meaning": "개인 보유역량 응답은 아직 없으므로 개인 전이율로 승인 불가"},
            {"component": "learning_path_auxiliary", "meaning": "학습모듈은 목표/필요지식/수행내용/평가 기준 확인용 보조 자료"},
        ],
    }


def review_questions() -> list[str]:
    return [
        "공통 KSA가 실제 이동 가능한 역량인지, 아니면 범용 문구라서 전이율을 낮춰야 하는지 확인한다.",
        "목표 직무의 target_only_gap_concepts가 교육훈련 보완 대상인지 확인한다.",
        "보고서 기준의 수평/수직 이동 유형과 경력개발경로 수준이 맞는지 확인한다.",
        "학습모듈 목표, 필요지식, 수행내용, 평가준거가 해당 보정 판단을 보조하는지 확인한다.",
        "확정 전까지 DB의 human_reviewed/accepted/reviewed 상태를 쓰지 않는다.",
    ]


def csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text[:1] in {"=", "+", "-", "@"} or text.lstrip()[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def write_safe_row(writer: csv.DictWriter, row: dict[str, Any], fieldnames: list[str]) -> None:
    writer.writerow({key: csv_cell(row.get(key)) for key in fieldnames})


def write_matrix_csv(pairs: list[dict[str, Any]]) -> None:
    fieldnames = [
        "source_unit_code",
        "source_unit_name",
        "source_level",
        "target_unit_code",
        "target_unit_name",
        "target_level",
        "movement_type",
        "level_delta",
        "exact_ksa_overlap_ratio",
        "shared_ksa_concept_count",
        "target_ksa_concept_count",
        "target_only_ksa_concept_count",
        "task_similarity_max",
        "task_similarity_link_count",
        "report_movement_component",
        "report_grounded_transferability_ratio",
        "review_priority",
    ]
    with MATRIX_CSV.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for pair in pairs:
            write_safe_row(writer, pair, fieldnames)


def write_packet_csv(pairs: list[dict[str, Any]]) -> None:
    fieldnames = [
        "source_unit_name",
        "target_unit_name",
        "movement_type",
        "source_level",
        "target_level",
        "exact_ksa_overlap_ratio",
        "task_similarity_max",
        "report_grounded_transferability_ratio",
        "shared_ksa_concepts",
        "target_gap_concepts",
        "source_module_goal_page",
        "target_module_goal_page",
        "review_decision",
        "reviewer",
        "review_rationale",
        "ontology_correction_action",
    ]
    with PACKET_CSV.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for pair in pairs:
            write_safe_row(
                writer,
                {
                    "source_unit_name": pair["source_unit_name"],
                    "target_unit_name": pair["target_unit_name"],
                    "movement_type": pair["movement_type"],
                    "source_level": pair["source_level"],
                    "target_level": pair["target_level"],
                    "exact_ksa_overlap_ratio": pair["exact_ksa_overlap_ratio"],
                    "task_similarity_max": pair["task_similarity_max"],
                    "report_grounded_transferability_ratio": pair["report_grounded_transferability_ratio"],
                    "shared_ksa_concepts": "; ".join(item["concept_name"] for item in pair["shared_ksa_concepts"][:12]),
                    "target_gap_concepts": "; ".join(item["concept_name"] for item in pair["target_only_gap_concepts"][:12]),
                    "source_module_goal_page": snippet_page(pair, "source_learning_module_context", "module_goal"),
                    "target_module_goal_page": snippet_page(pair, "target_learning_module_context", "module_goal"),
                    "review_decision": "",
                    "reviewer": "",
                    "review_rationale": "",
                    "ontology_correction_action": "",
                },
                fieldnames,
            )


def snippet_page(pair: dict[str, Any], context_key: str, snippet_key: str) -> Any:
    context = pair.get(context_key) or {}
    return ((context.get("snippets") or {}).get(snippet_key) or {}).get("page")


def write_packet_md(packet: dict[str, Any]) -> None:
    lines = [
        "# HR Transferability Human Review Packet",
        "",
        "- Scope: 인사 `02020201`",
        "- Status: `review_required`",
        "- Approval ready: `false`",
        "- DB writes: `false`",
        "- Learning modules: OCR auxiliary review evidence only",
        "",
        "## Report Movement Model",
        "",
        "| component | review meaning |",
        "|---|---|",
    ]
    for component in packet["report_movement_model"]["movement_components"]:
        lines.append(f"| {component['component']} | {component['meaning']} |")
    lines.extend(
        [
            "",
            "## Unit Learning Path",
            "",
            "| unit | level | career | concepts | KSA types | module status | top module terms |",
            "|---|---:|---|---:|---|---|---|",
        ]
    )
    for unit in packet["review_units"]:
        card = unit.get("learning_module_context") or {}
        terms = ", ".join(f"{item['term']}:{item['count']}" for item in (card.get("top_terms") or [])[:5])
        career = (unit.get("career_path") or {}).get("position_name") or ""
        lines.append(
            f"| {unit['unit_name']} | {unit['level']} | {career} | {unit['concept_count']} | "
            f"{unit['concept_type_summary']} | {card.get('status')} | {terms} |"
        )
    lines.extend(
        [
            "",
            "## Selected Transferability Review Pairs",
            "",
            "| source -> target | move | exact KSA | task | report ratio | shared KSA | target gap sample | review status |",
            "|---|---|---:|---:|---:|---|---|---|",
        ]
    )
    for pair in packet["selected_review_pairs"]:
        shared = ", ".join(item["concept_name"] for item in pair["shared_ksa_concepts"][:6])
        gap = ", ".join(item["concept_name"] for item in pair["target_only_gap_concepts"][:6])
        lines.append(
            f"| {pair['source_unit_name']} -> {pair['target_unit_name']} | {pair['movement_type']} | "
            f"{pair['exact_ksa_overlap_ratio']} | {pair['task_similarity_max']} | "
            f"{pair['report_grounded_transferability_ratio']} | {shared} | {gap} | pending_human_review |"
        )
    lines.extend(["", "## Pair Evidence Details", ""])
    for pair in packet["selected_review_pairs"]:
        lines.extend(pair_detail_lines(pair))
    lines.extend(["## Review Questions", ""])
    for question in packet["review_questions"]:
        lines.append(f"- {question}")
    PACKET_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def pair_detail_lines(pair: dict[str, Any]) -> list[str]:
    target_goal = ((pair.get("target_learning_module_context") or {}).get("snippets") or {}).get("module_goal") or {}
    goal_snippet = str(target_goal.get("snippet") or "")
    if len(goal_snippet) > 260:
        goal_snippet = goal_snippet[:260].rstrip() + "..."
    lines = [
        f"### {pair['source_unit_name']} -> {pair['target_unit_name']}",
        f"- Movement: {pair['movement_type']} / level {pair['source_level']} -> {pair['target_level']}",
        (
            f"- Transferability review ratio: {pair['report_grounded_transferability_ratio']} "
            f"(exact KSA {pair['exact_ksa_overlap_ratio']}, task {pair['task_similarity_max']}, "
            f"report movement component {pair['report_movement_component']})"
        ),
        f"- Shared KSA concepts: {', '.join(item['concept_name'] for item in pair['shared_ksa_concepts'][:12]) or '-'}",
        f"- Target-only gap concepts: {', '.join(item['concept_name'] for item in pair['target_only_gap_concepts'][:12]) or '-'}",
        f"- Target module goal page {target_goal.get('page') or '-'}: {goal_snippet}",
    ]
    if pair.get("task_similarity_examples"):
        example = pair["task_similarity_examples"][0]
        lines.append(
            f"- Top task evidence: {example.get('source_element')} -> {example.get('target_element')} / "
            f"score {example.get('similarity_score')}"
        )
    lines.extend(["- Human decision: pending", ""])
    return lines


def write_packet_html(packet: dict[str, Any]) -> None:
    lines = [
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">',
        "<title>HR Transferability Human Review Packet</title>",
        (
            "<style>body{font-family:Arial,'Malgun Gothic',sans-serif;margin:24px;color:#1f2933;background:#fafafa}"
            "table{border-collapse:collapse;width:100%;background:#fff}th,td{border:1px solid #d9dee7;padding:8px;"
            "vertical-align:top;font-size:13px}th{background:#eef2f6}h1,h2{margin-top:24px}"
            ".badge{display:inline-block;padding:2px 6px;border-radius:4px;background:#f7dfb7}"
            ".pending{color:#9a3412;font-weight:600}.small{font-size:12px;color:#52606d}"
            ".pair{margin:16px 0;padding:12px;border:1px solid #d9dee7;background:#fff}</style>"
        ),
        "</head><body>",
        "<h1>HR Transferability Human Review Packet</h1>",
        (
            '<p class="small">Scope: 인사 02020201. Approval ready: false. '
            "DB writes: false. Learning modules are auxiliary review evidence only.</p>"
        ),
        "<h2>Selected Review Pairs</h2>",
        (
            "<table><thead><tr><th>Source -> Target</th><th>Move</th><th>Exact KSA</th><th>Task</th>"
            "<th>Report Ratio</th><th>Shared KSA</th><th>Target Gap</th><th>Decision</th></tr></thead><tbody>"
        ),
    ]
    for pair in packet["selected_review_pairs"]:
        lines.append("<tr>")
        lines.append(
            f"<td>{html.escape(pair['source_unit_name'])} -> {html.escape(pair['target_unit_name'])}</td>"
        )
        lines.append(f"<td>{html.escape(pair['movement_type'])}</td>")
        lines.append(
            f"<td>{pair['exact_ksa_overlap_ratio']}</td><td>{pair['task_similarity_max']}</td>"
            f"<td>{pair['report_grounded_transferability_ratio']}</td>"
        )
        lines.append(
            f"<td>{html.escape(', '.join(item['concept_name'] for item in pair['shared_ksa_concepts'][:8]))}</td>"
        )
        lines.append(
            f"<td>{html.escape(', '.join(item['concept_name'] for item in pair['target_only_gap_concepts'][:8]))}</td>"
        )
        lines.append('<td class="pending">pending human review</td>')
        lines.append("</tr>")
    lines.extend(["</tbody></table>", "<h2>Evidence Details</h2>"])
    for pair in packet["selected_review_pairs"]:
        target_goal = ((pair.get("target_learning_module_context") or {}).get("snippets") or {}).get("module_goal") or {}
        target_goal_text = compact_text(str(target_goal.get("snippet") or ""))[:500]
        lines.append('<div class="pair">')
        lines.append(f"<h3>{html.escape(pair['source_unit_name'])} -> {html.escape(pair['target_unit_name'])}</h3>")
        lines.append(
            f'<p><span class="badge">{html.escape(pair["movement_type"])}</span> '
            f"level {pair['source_level']} -> {pair['target_level']} / "
            f"ratio {pair['report_grounded_transferability_ratio']}</p>"
        )
        lines.append(
            f"<p><b>Shared KSA:</b> "
            f"{html.escape(', '.join(item['concept_name'] for item in pair['shared_ksa_concepts'][:12]) or '-')}</p>"
        )
        lines.append(
            f"<p><b>Target gap:</b> "
            f"{html.escape(', '.join(item['concept_name'] for item in pair['target_only_gap_concepts'][:12]) or '-')}</p>"
        )
        lines.append(
            f"<p><b>Target module goal page {target_goal.get('page') or '-'}:</b> "
            f"{html.escape(target_goal_text)}</p>"
        )
        lines.append('<p class="pending">Human decision: pending. No DB write.</p>')
        lines.append("</div>")
    lines.append("</body></html>")
    PACKET_HTML.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build HR transferability Human Review artifacts without DB writes "
            "or approval/status mutations."
        )
    )
    parser.add_argument("--date-stamp", default=DEFAULT_DATE_STAMP)
    parser.add_argument("--reports-dir", type=Path)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--main-json", type=Path)
    parser.add_argument("--main-md", type=Path)
    parser.add_argument("--manifest-json", type=Path)
    parser.add_argument("--cards-json", type=Path)
    parser.add_argument("--cards-md", type=Path)
    parser.add_argument("--packet-json", type=Path)
    parser.add_argument("--packet-md", type=Path)
    parser.add_argument("--packet-csv", type=Path)
    parser.add_argument("--packet-html", type=Path)
    parser.add_argument("--matrix-csv", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    configure_artifact_paths(
        date_stamp=args.date_stamp,
        reports_dir=args.reports_dir,
        db_path=args.db_path,
        main_json=args.main_json,
        main_md=args.main_md,
        manifest_json=args.manifest_json,
        cards_json=args.cards_json,
        cards_md=args.cards_md,
        packet_json=args.packet_json,
        packet_md=args.packet_md,
        packet_csv=args.packet_csv,
        packet_html=args.packet_html,
        matrix_csv=args.matrix_csv,
    )
    cards_payload = build_ocr_cards()
    main_artifact = update_main_artifact(cards_payload)
    packet = build_packet(cards_payload, main_artifact)
    print(
        json.dumps(
            {
                "updated": [
                    str(MAIN_JSON.relative_to(ROOT)),
                    str(MAIN_MD.relative_to(ROOT)),
                    str(CARDS_JSON.relative_to(ROOT)),
                    str(CARDS_MD.relative_to(ROOT)),
                ],
                "created": [
                    str(PACKET_JSON.relative_to(ROOT)),
                    str(PACKET_MD.relative_to(ROOT)),
                    str(PACKET_CSV.relative_to(ROOT)),
                    str(PACKET_HTML.relative_to(ROOT)),
                    str(MATRIX_CSV.relative_to(ROOT)),
                ],
                "summary": packet["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
