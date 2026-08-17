from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Iterable


KNOWLEDGE_GRAPH_SCHEMA = "ncs_knowledge_graph_v1"
DEFAULT_NODE_LIMIT = 72
MAX_NODE_LIMIT = 120
TRUSTED_REVIEW_STATUSES = {"human_reviewed", "accepted", "reviewed"}
CORE_TABLES = (
    "classifications",
    "competency_units",
    "competency_elements",
    "performance_criteria",
    "ontology_concepts",
    "criteria_concept_links",
)


class KnowledgeGraphDataError(RuntimeError):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        missing_tables: Iterable[str] = (),
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.missing_tables = tuple(missing_tables)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "schema": KNOWLEDGE_GRAPH_SCHEMA,
            "error": self.code,
            "detail": self.detail,
            "read_only": True,
            "db_writes": False,
            "approval_claim": False,
        }
        if self.missing_tables:
            payload["missing_tables"] = list(self.missing_tables)
        return payload


def _connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise KnowledgeGraphDataError(
            "database_missing",
            "The prepared NCS database is not available.",
        )
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn
    except sqlite3.Error as exc:
        raise KnowledgeGraphDataError(
            "database_unreadable",
            f"The prepared NCS database cannot be opened read-only: {type(exc).__name__}",
        ) from exc


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _clamp_node_limit(value: int | str | None) -> int:
    try:
        parsed = int(value if value is not None else DEFAULT_NODE_LIMIT)
    except (TypeError, ValueError):
        parsed = DEFAULT_NODE_LIMIT
    return max(24, min(parsed, MAX_NODE_LIMIT))


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _clean_text(value: Any, *, limit: int | None = None) -> str:
    text = " ".join(str(value or "").split())
    if limit is not None and len(text) > limit:
        return text[: max(limit - 3, 1)].rstrip() + "..."
    return text


def _unit_payload(row: sqlite3.Row) -> dict[str, Any]:
    hierarchy = [
        {"code": row["major_code"], "name": row["major_name"]},
        {"code": row["middle_code"], "name": row["middle_name"]},
        {"code": row["small_code"], "name": row["small_name"]},
        {"code": row["sub_code"], "name": row["sub_name"]},
    ]
    return {
        "unit_code": row["unit_code"],
        "label": row["unit_name"],
        "level": row["unit_level"],
        "hierarchy": hierarchy,
        "hierarchy_label": " > ".join(
            f"{part['code']} {part['name']}" for part in hierarchy
        ),
    }


def _search_units(
    conn: sqlite3.Connection,
    *,
    query: str,
    unit_code: str,
    limit: int = 24,
) -> tuple[list[sqlite3.Row], str]:
    select_sql = """
        SELECT
            cu.unit_code,
            COALESCE(NULLIF(TRIM(cu.unit_name_refined), ''), cu.unit_name_raw) AS unit_name,
            COALESCE(NULLIF(TRIM(cu.api_unit_level), ''), cu.unit_level_raw) AS unit_level,
            cu.review_status AS unit_review_status,
            c.classification_id,
            c.major_code, c.major_name,
            c.middle_code, c.middle_name,
            c.small_code, c.small_name,
            c.sub_code, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
    """
    if unit_code:
        rows = conn.execute(
            select_sql + " WHERE cu.unit_code = ? LIMIT 1",
            (unit_code,),
        ).fetchall()
        return rows, "unit_code"

    if query:
        pattern = _like_pattern(query)
        prefix_pattern = pattern[1:]
        name_expr = "COALESCE(NULLIF(TRIM(cu.unit_name_refined), ''), cu.unit_name_raw)"
        rows = conn.execute(
            select_sql
            + f"""
              WHERE cu.unit_code = ?
                 OR {name_expr} = ?
                 OR {name_expr} LIKE ? ESCAPE '\\'
              ORDER BY
                CASE WHEN cu.unit_code = ? THEN 0
                     WHEN {name_expr} = ? THEN 1
                     ELSE 2 END,
                cu.unit_code
              LIMIT ?
            """,
            (
                query,
                query,
                prefix_pattern,
                query,
                query,
                limit,
            ),
        ).fetchall()
        exact_rows = [
            row
            for row in rows
            if str(row["unit_code"] or "") == query
            or str(row["unit_name"] or "").strip() == query
        ]
        if exact_rows:
            return exact_rows, "query_exact"
        contains_rows = conn.execute(
            select_sql
            + f"""
              WHERE {name_expr} LIKE ? ESCAPE '\\'
                 OR c.major_name LIKE ? ESCAPE '\\'
                 OR c.middle_name LIKE ? ESCAPE '\\'
                 OR c.small_name LIKE ? ESCAPE '\\'
                 OR c.sub_name LIKE ? ESCAPE '\\'
              ORDER BY
                CASE WHEN {name_expr} LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END,
                cu.unit_code
              LIMIT ?
            """,
            (pattern, pattern, pattern, pattern, pattern, pattern, limit * 2),
        ).fetchall()
        combined: list[sqlite3.Row] = []
        seen: set[str] = set()
        for row in [*rows, *contains_rows]:
            code = str(row["unit_code"])
            if code in seen:
                continue
            seen.add(code)
            combined.append(row)
            if len(combined) >= limit:
                break
        return combined, "query_prefix_and_contains"

    return [], "query_missing"


class _GraphBuilder:
    def __init__(self, *, max_nodes: int) -> None:
        self.max_nodes = max_nodes
        self.max_edges = max(max_nodes * 3, 72)
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.omitted: Counter[str] = Counter()

    def add_node(self, node: dict[str, Any]) -> bool:
        node_id = str(node["id"])
        if node_id in self.nodes:
            return True
        if len(self.nodes) >= self.max_nodes:
            self.omitted[str(node.get("type") or "unknown")] += 1
            return False
        self.nodes[node_id] = node
        return True

    def add_edge(self, edge: dict[str, Any]) -> bool:
        source = str(edge["source"])
        target = str(edge["target"])
        if source not in self.nodes or target not in self.nodes:
            return False
        edge_id = str(edge.get("id") or f"{source}|{edge['type']}|{target}")
        if edge_id in self.edges:
            existing = self.edges[edge_id]
            refs = existing.setdefault(
                "evidence_refs",
                [existing["source_ref"]] if existing.get("source_ref") else [],
            )
            incoming_ref = edge.get("source_ref")
            if incoming_ref and incoming_ref not in refs:
                refs.append(incoming_ref)
            existing["evidence_count"] = len(refs)

            evidence_items = existing.setdefault(
                "evidence_items",
                [existing["evidence"]] if existing.get("evidence") else [],
            )
            incoming_evidence = edge.get("evidence")
            if incoming_evidence and incoming_evidence not in evidence_items:
                evidence_items.append(incoming_evidence)

            review_states = existing.setdefault(
                "review_states",
                [existing["review_state"]] if existing.get("review_state") else [],
            )
            incoming_state = edge.get("review_state")
            if incoming_state and incoming_state not in review_states:
                review_states.append(incoming_state)

            strength_rank = {
                "direct_structure": 6,
                "direct": 5,
                "task_evidence": 4,
                "candidate_relation": 3,
                "supporting": 2,
                "inherited": 1,
            }
            if strength_rank.get(str(edge.get("strength")), 0) > strength_rank.get(
                str(existing.get("strength")), 0
            ):
                existing["strength"] = edge.get("strength")
            if edge.get("confidence") is not None:
                existing["confidence"] = max(
                    float(existing.get("confidence") or 0.0),
                    float(edge["confidence"]),
                )
            return True
        if len(self.edges) >= self.max_edges:
            self.omitted["edge"] += 1
            return False
        edge["id"] = edge_id
        self.edges[edge_id] = edge
        return True

    def note_omitted(self, node_type: str, count: int) -> None:
        if count > 0:
            self.omitted[node_type] += int(count)


def _node(
    node_id: str,
    node_type: str,
    label: str,
    *,
    subtitle: str = "",
    layer: int,
    source_table: str,
    source_key: str | int,
    properties: dict[str, Any] | None = None,
    review_state: str = "",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "label": _clean_text(label, limit=110),
        "subtitle": _clean_text(subtitle, limit=180),
        "layer": layer,
        "source": {"table": source_table, "key": str(source_key)},
        "properties": properties or {},
        "review_state": review_state or "not_applicable",
    }


def _edge(
    source: str,
    target: str,
    relation_type: str,
    label: str,
    *,
    source_table: str,
    source_key: str | int,
    strength: str = "direct",
    confidence: float | None = None,
    review_state: str = "",
    evidence: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": source,
        "target": target,
        "type": relation_type,
        "label": label,
        "strength": strength,
        "source_ref": {"table": source_table, "key": str(source_key)},
        "review_state": review_state or "not_applicable",
    }
    if confidence is not None:
        payload["confidence"] = round(float(confidence), 4)
    if evidence:
        payload["evidence"] = _clean_text(evidence, limit=240)
    return payload


def _fetch_elements(
    conn: sqlite3.Connection,
    unit_code: str,
    *,
    limit: int,
) -> tuple[list[sqlite3.Row], int]:
    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM competency_elements WHERE unit_code = ?",
            (unit_code,),
        ).fetchone()[0]
    )
    rows = conn.execute(
        """
        SELECT
            element_id, element_no, element_code_raw,
            COALESCE(NULLIF(TRIM(element_name_refined), ''), element_name_raw) AS element_name,
            COALESCE(NULLIF(TRIM(api_element_level), ''), element_level_raw) AS element_level,
            review_status
        FROM competency_elements
        WHERE unit_code = ?
        ORDER BY CAST(element_no AS INTEGER), element_id
        LIMIT ?
        """,
        (unit_code, limit),
    ).fetchall()
    return rows, total


def _fetch_tasks(
    conn: sqlite3.Connection,
    element_ids: list[int],
    *,
    per_element: int = 3,
) -> tuple[list[sqlite3.Row], int]:
    if not element_ids:
        return [], 0
    placeholders = ",".join("?" for _ in element_ids)
    rows = conn.execute(
        f"""
        SELECT
            criteria_id, element_id, criteria_no,
            COALESCE(NULLIF(TRIM(criteria_text_refined), ''), criteria_text_raw) AS criteria_text,
            criteria_text_raw,
            review_status
        FROM performance_criteria
        WHERE element_id IN ({placeholders})
        ORDER BY element_id, CAST(criteria_no AS INTEGER), criteria_id
        """,
        element_ids,
    ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[int(row["element_id"])].append(row)
    selected = [
        row
        for element_id in element_ids
        for row in grouped.get(element_id, [])[:per_element]
    ]
    return selected, len(rows)


def _fetch_concepts_for_tasks(
    conn: sqlite3.Connection,
    task_ids: list[int],
    *,
    limit: int,
) -> tuple[list[sqlite3.Row], int]:
    if not task_ids:
        return [], 0
    placeholders = ",".join("?" for _ in task_ids)
    rows = conn.execute(
        f"""
        SELECT
            oc.concept_id, oc.concept_name, oc.concept_type,
            oc.definition, oc.definition_status, oc.review_status,
            COUNT(DISTINCT ccl.criteria_id) AS task_count
        FROM criteria_concept_links ccl
        JOIN ontology_concepts oc ON oc.concept_id = ccl.concept_id
        WHERE ccl.criteria_id IN ({placeholders})
          AND LOWER(COALESCE(ccl.link_status, '')) <> 'rejected'
        GROUP BY oc.concept_id
        ORDER BY task_count DESC, oc.concept_type, oc.concept_id
        """,
        task_ids,
    ).fetchall()
    per_type_limit = max(4, min(10, limit))
    type_counts: Counter[str] = Counter()
    selected: list[sqlite3.Row] = []
    for row in rows:
        concept_type = _concept_type(str(row["concept_type"] or ""))
        if type_counts[concept_type] >= per_type_limit:
            continue
        selected.append(row)
        type_counts[concept_type] += 1
        if len(selected) >= limit:
            break
    return selected, len(rows)


def _concept_type(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"knowledge", "skill", "attitude"}:
        return normalized
    return "ksa"


def _add_concept_relations(
    conn: sqlite3.Connection,
    graph: _GraphBuilder,
    *,
    tables: set[str],
    concept_ids: list[int],
    task_ids: list[int],
) -> None:
    if len(concept_ids) < 2:
        return
    concept_placeholders = ",".join("?" for _ in concept_ids)
    if "ontology_concept_relations" in tables:
        rows = conn.execute(
            f"""
            SELECT relation_id, source_concept_id, target_concept_id,
                   relation_type, relation_label, review_status
            FROM ontology_concept_relations
            WHERE source_concept_id IN ({concept_placeholders})
              AND target_concept_id IN ({concept_placeholders})
              AND LOWER(COALESCE(review_status, '')) <> 'rejected'
            ORDER BY relation_id
            """,
            [*concept_ids, *concept_ids],
        ).fetchall()
        graph.note_omitted("edge:concept_relation", max(len(rows) - 36, 0))
        for row in rows[:36]:
            graph.add_edge(
                _edge(
                    f"concept:{row['source_concept_id']}",
                    f"concept:{row['target_concept_id']}",
                    row["relation_type"],
                    row["relation_label"] or row["relation_type"],
                    source_table="ontology_concept_relations",
                    source_key=row["relation_id"],
                    strength="candidate_relation",
                    review_state=row["review_status"],
                )
            )
    if "task_ksa_concept_relations" not in tables or not task_ids:
        return
    task_placeholders = ",".join("?" for _ in task_ids)
    rows = conn.execute(
        f"""
        SELECT
            MIN(relation_id) AS relation_id,
            source_concept_id, target_concept_id, relation_type,
            AVG(confidence_score) AS confidence_score,
            MIN(review_status) AS review_status,
            COUNT(*) AS evidence_count
        FROM task_ksa_concept_relations
        WHERE criteria_id IN ({task_placeholders})
          AND source_concept_id IN ({concept_placeholders})
          AND target_concept_id IN ({concept_placeholders})
          AND LOWER(COALESCE(review_status, '')) <> 'rejected'
        GROUP BY source_concept_id, target_concept_id, relation_type
        ORDER BY evidence_count DESC, confidence_score DESC
        """,
        [*task_ids, *concept_ids, *concept_ids],
    ).fetchall()
    graph.note_omitted("edge:task_evidence", max(len(rows) - 36, 0))
    for row in rows[:36]:
        graph.add_edge(
            _edge(
                f"concept:{row['source_concept_id']}",
                f"concept:{row['target_concept_id']}",
                row["relation_type"],
                row["relation_type"],
                source_table="task_ksa_concept_relations",
                source_key=row["relation_id"],
                strength="task_evidence",
                confidence=row["confidence_score"],
                review_state=row["review_status"],
                evidence=f"같은 수행준거에서 확인된 근거 {row['evidence_count']}건",
            )
        )


def _add_courses(
    conn: sqlite3.Connection,
    graph: _GraphBuilder,
    *,
    tables: set[str],
    unit_code: str,
    concept_ids: list[int],
    limit: int = 8,
) -> None:
    required = {"ncs_training_courses", "ncs_training_course_unit_links"}
    if not required.issubset(tables):
        return
    rows = conn.execute(
        """
        SELECT
            tc.training_course_id, tc.compe_unit_name, tc.compe_unit_level,
            tc.train_goal, tc.train_time, tc.fac_name, tc.meth_name,
            l.link_id, l.link_method, l.confidence_score, l.review_status
        FROM ncs_training_course_unit_links l
        JOIN ncs_training_courses tc
          ON tc.training_course_id = l.training_course_id
        WHERE l.unit_code = ?
          AND LOWER(COALESCE(l.review_status, '')) <> 'rejected'
        ORDER BY l.confidence_score DESC, tc.training_course_id
        LIMIT ?
        """,
        (unit_code, limit),
    ).fetchall()
    total = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT training_course_id)
            FROM ncs_training_course_unit_links
            WHERE unit_code = ?
              AND LOWER(COALESCE(review_status, '')) <> 'rejected'
            """,
            (unit_code,),
        ).fetchone()[0]
    )
    graph.note_omitted("course", max(total - len(rows), 0))
    course_ids: list[int] = []
    for row in rows:
        course_id = int(row["training_course_id"])
        course_ids.append(course_id)
        label = _clean_text(row["compe_unit_name"]) or f"훈련과정 {course_id}"
        graph.add_node(
            _node(
                f"course:{course_id}",
                "course",
                label,
                subtitle=_clean_text(row["train_goal"], limit=100),
                layer=5,
                source_table="ncs_training_courses",
                source_key=course_id,
                properties={
                    "level": _clean_text(row["compe_unit_level"]),
                    "hours": _clean_text(row["train_time"]),
                    "methods": _clean_text(row["meth_name"]),
                    "facilities": _clean_text(row["fac_name"]),
                    "goal": _clean_text(row["train_goal"], limit=280),
                },
                review_state=row["review_status"],
            )
        )
        graph.add_edge(
            _edge(
                f"unit:{unit_code}",
                f"course:{course_id}",
                "training_for_unit",
                "훈련으로 연결",
                source_table="ncs_training_course_unit_links",
                source_key=row["link_id"],
                confidence=row["confidence_score"],
                review_state=row["review_status"],
                evidence=row["link_method"],
            )
        )
    if (
        not course_ids
        or not concept_ids
        or "ncs_training_course_concept_links" not in tables
    ):
        return
    course_placeholders = ",".join("?" for _ in course_ids)
    concept_placeholders = ",".join("?" for _ in concept_ids)
    link_rows = conn.execute(
        f"""
        SELECT link_id, training_course_id, concept_id, link_method,
               confidence_score, evidence_text, review_status
        FROM ncs_training_course_concept_links
        WHERE training_course_id IN ({course_placeholders})
          AND concept_id IN ({concept_placeholders})
          AND LOWER(COALESCE(review_status, '')) <> 'rejected'
        ORDER BY confidence_score DESC, link_id
        """,
        [*course_ids, *concept_ids],
    ).fetchall()
    graph.note_omitted("edge:course_concept", max(len(link_rows) - 48, 0))
    for row in link_rows[:48]:
        link_method = str(row["link_method"] or "")
        strength = (
            "direct"
            if link_method == "training_goal_concept_text"
            else "supporting"
            if "token" in link_method or "element" in link_method
            else "inherited"
        )
        graph.add_edge(
            _edge(
                f"concept:{row['concept_id']}",
                f"course:{row['training_course_id']}",
                "course_covers_concept",
                "과정이 다룸",
                source_table="ncs_training_course_concept_links",
                source_key=row["link_id"],
                strength=strength,
                confidence=row["confidence_score"],
                review_state=row["review_status"],
                evidence=row["evidence_text"] or link_method,
            )
        )


def _add_supporting_evidence(
    conn: sqlite3.Connection,
    graph: _GraphBuilder,
    *,
    tables: set[str],
    unit_code: str,
) -> None:
    if {"ncs_qualification_items", "ncs_unit_qualification_links"}.issubset(tables):
        rows = conn.execute(
            """
            SELECT l.link_id, l.jm_cd, q.jm_nm, q.exam_insti_nm,
                   l.link_method, l.confidence_score, l.review_status
            FROM ncs_unit_qualification_links l
            JOIN ncs_qualification_items q ON q.jm_cd = l.jm_cd
            WHERE l.unit_code = ?
              AND LOWER(COALESCE(l.review_status, '')) <> 'rejected'
            ORDER BY l.confidence_score DESC, q.jm_nm
            """,
            (unit_code,),
        ).fetchall()
        graph.note_omitted("qualification", max(len(rows) - 4, 0))
        for row in rows[:4]:
            graph.add_node(
                _node(
                    f"qualification:{row['jm_cd']}",
                    "qualification",
                    row["jm_nm"],
                    subtitle=row["exam_insti_nm"] or "자격 보조 근거",
                    layer=5,
                    source_table="ncs_qualification_items",
                    source_key=row["jm_cd"],
                    properties={
                        "official_recognition_claim": False,
                        "role": "supporting_evidence_only",
                    },
                    review_state=row["review_status"],
                )
            )
            graph.add_edge(
                _edge(
                    f"unit:{unit_code}",
                    f"qualification:{row['jm_cd']}",
                    "qualification_context",
                    "관련 자격 참고",
                    source_table="ncs_unit_qualification_links",
                    source_key=row["link_id"],
                    strength="supporting",
                    confidence=row["confidence_score"],
                    review_state=row["review_status"],
                    evidence=row["link_method"],
                )
            )
    if {"ncs_job_base_competencies", "ncs_unit_job_base_links"}.issubset(tables):
        rows = conn.execute(
            """
            SELECT
                MIN(l.link_id) AS link_id,
                j.job_base_competency_id, j.competency_name,
                MAX(l.confidence_score) AS confidence_score,
                MIN(l.review_status) AS review_status,
                MIN(l.link_method) AS link_method
            FROM ncs_unit_job_base_links l
            JOIN ncs_job_base_competencies j
              ON j.job_base_competency_id = l.job_base_competency_id
            WHERE l.unit_code = ?
              AND LOWER(COALESCE(l.review_status, '')) <> 'rejected'
            GROUP BY j.job_base_competency_id, j.competency_name
            ORDER BY confidence_score DESC, j.competency_name
            """,
            (unit_code,),
        ).fetchall()
        graph.note_omitted("job_base", max(len(rows) - 4, 0))
        for row in rows[:4]:
            node_id = f"job_base:{row['job_base_competency_id']}"
            graph.add_node(
                _node(
                    node_id,
                    "job_base",
                    row["competency_name"],
                    subtitle="직업기초능력 보조 근거",
                    layer=5,
                    source_table="ncs_job_base_competencies",
                    source_key=row["job_base_competency_id"],
                    properties={"role": "supporting_evidence_only"},
                    review_state=row["review_status"],
                )
            )
            graph.add_edge(
                _edge(
                    f"unit:{unit_code}",
                    node_id,
                    "job_base_context",
                    "기초역량 참고",
                    source_table="ncs_unit_job_base_links",
                    source_key=row["link_id"],
                    strength="supporting",
                    confidence=row["confidence_score"],
                    review_state=row["review_status"],
                    evidence=row["link_method"],
                )
            )


def _graph_response(
    db_path: str | Path,
    graph: _GraphBuilder,
    *,
    mode: str,
    query: dict[str, Any],
    focus: dict[str, Any],
    candidates: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    node_types = Counter(node["type"] for node in graph.nodes.values())
    edge_types = Counter(edge["type"] for edge in graph.edges.values())
    stat = Path(db_path).stat()
    return {
        "ok": True,
        "schema": KNOWLEDGE_GRAPH_SCHEMA,
        "mode": mode,
        "selection_required": False,
        "query": query,
        "focus": focus,
        "candidates": candidates or [],
        "nodes": list(graph.nodes.values()),
        "edges": list(graph.edges.values()),
        "facets": {
            "node_types": dict(sorted(node_types.items())),
            "edge_types": dict(sorted(edge_types.items())),
        },
        "truncation": {
            "truncated": bool(graph.omitted),
            "node_limit": graph.max_nodes,
            "edge_limit": graph.max_edges,
            "omitted_by_type": dict(sorted(graph.omitted.items())),
        },
        "data_version": {
            "db_modified_at": datetime.fromtimestamp(
                stat.st_mtime,
                tz=timezone.utc,
            ).isoformat(),
            "contract": KNOWLEDGE_GRAPH_SCHEMA,
        },
        "read_only": True,
        "db_writes": False,
        "approval_claim": False,
        "active_product_scope": "NCS",
        "audit": {
            "read_only": True,
            "sqf_used": False,
            "learning_modules_used": False,
            "framework_reference_scored": False,
        },
        "provenance_policy": {
            "local_ncs_sqlite_only": True,
            "external_content_used": False,
            "source_payload_exposed": False,
            "trusted_definition_required_for_display": True,
        },
        "warnings": warnings
        or [
            "그래프 관계는 교육훈련 탐색 근거이며 공식 자격·법적 적격성 판단이 아닙니다.",
            "자동·후보 관계는 사람의 승인 상태로 해석하지 않습니다.",
        ],
    }


def _build_ncs_overview(
    conn: sqlite3.Connection,
    db_path: str | Path,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        WITH major_classifications AS (
            SELECT
                major_code,
                MIN(major_name) AS major_name,
                COUNT(*) AS classification_count
            FROM classifications
            WHERE TRIM(COALESCE(major_code, '')) <> ''
            GROUP BY major_code
        ),
        major_units AS (
            SELECT c.major_code, COUNT(*) AS unit_count
            FROM competency_units cu
            JOIN classifications c
              ON c.classification_id = cu.classification_id
            GROUP BY c.major_code
        )
        SELECT
            mc.major_code,
            mc.major_name,
            mc.classification_count,
            COALESCE(mu.unit_count, 0) AS unit_count
        FROM major_classifications mc
        LEFT JOIN major_units mu ON mu.major_code = mc.major_code
        ORDER BY mc.major_code
        """
    ).fetchall()
    graph = _GraphBuilder(max_nodes=64)
    total_units = sum(int(row["unit_count"] or 0) for row in rows)
    total_classifications = sum(int(row["classification_count"] or 0) for row in rows)
    root_id = "overview:ncs"
    graph.add_node(
        _node(
            root_id,
            "overview",
            "NCS 전체",
            subtitle=f"{len(rows)}개 대분류 · {total_units:,}개 능력단위",
            layer=0,
            source_table="classifications",
            source_key="all",
            properties={
                "major_count": len(rows),
                "classification_count": total_classifications,
                "unit_count": total_units,
                "navigation": "대분류 노드를 선택해 전체 분류체계를 펼치세요.",
            },
        )
    )
    for row in rows:
        major_code = str(row["major_code"])
        major_id = f"major:{major_code}"
        graph.add_node(
            _node(
                major_id,
                "major",
                row["major_name"],
                subtitle=f"{major_code} · {int(row['unit_count'] or 0):,}개 능력단위",
                layer=1,
                source_table="classifications",
                source_key=major_code,
                properties={
                    "major_code": major_code,
                    "classification_count": int(row["classification_count"] or 0),
                    "unit_count": int(row["unit_count"] or 0),
                    "expandable": True,
                },
            )
        )
        graph.add_edge(
            _edge(
                root_id,
                major_id,
                "contains_major",
                "대분류",
                source_table="classifications",
                source_key=major_code,
            )
        )
    return _graph_response(
        db_path,
        graph,
        mode="overview",
        query={"text": "", "resolved_by": "all_ncs_overview"},
        focus={
            "node_id": root_id,
            "label": "NCS 전체 지식그래프",
            "hierarchy_label": f"{len(rows)}개 대분류 · {total_classifications:,}개 세분류 · {total_units:,}개 능력단위",
        },
        warnings=[
            f"첫 화면은 현재 DB의 NCS 대분류 {len(rows)}개 전체입니다. 대분류를 선택하면 하위 분류 전체를 펼칩니다.",
            f"능력단위 {total_units:,}개와 과업·KSA는 선택 영역만 단계적으로 불러와 브라우저 과부하를 방지합니다.",
        ],
    )


def _build_major_overview(
    conn: sqlite3.Connection,
    db_path: str | Path,
    *,
    major_code: str,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT
            c.classification_id,
            c.major_code, c.major_name,
            c.middle_code, c.middle_name,
            c.small_code, c.small_name,
            c.sub_code, c.sub_name,
            COUNT(cu.unit_code) AS unit_count
        FROM classifications c
        LEFT JOIN competency_units cu
          ON cu.classification_id = c.classification_id
        WHERE c.major_code = ?
        GROUP BY c.classification_id
        ORDER BY c.middle_code, c.small_code, c.sub_code, c.classification_id
        """,
        (major_code,),
    ).fetchall()
    if not rows:
        raise KnowledgeGraphDataError(
            "major_not_found",
            f"NCS 대분류 코드 {major_code!r}를 찾지 못했습니다.",
        )

    graph = _GraphBuilder(max_nodes=260)
    major_name = str(rows[0]["major_name"] or major_code)
    total_units = sum(int(row["unit_count"] or 0) for row in rows)
    major_id = f"major:{major_code}"
    graph.add_node(
        _node(
            major_id,
            "major",
            major_name,
            subtitle=f"NCS 대분류 {major_code} · {total_units:,}개 능력단위",
            layer=0,
            source_table="classifications",
            source_key=major_code,
            properties={
                "major_code": major_code,
                "classification_count": len(rows),
                "unit_count": total_units,
                "expandable": True,
            },
        )
    )

    middle_units: Counter[str] = Counter()
    small_units: Counter[str] = Counter()
    middle_labels: dict[str, str] = {}
    small_labels: dict[str, str] = {}
    for row in rows:
        middle_id = f"middle:{major_code}:{row['middle_code']}"
        small_id = f"small:{major_code}:{row['middle_code']}:{row['small_code']}"
        unit_count = int(row["unit_count"] or 0)
        middle_units[middle_id] += unit_count
        small_units[small_id] += unit_count
        middle_labels[middle_id] = str(row["middle_name"] or row["middle_code"])
        small_labels[small_id] = str(row["small_name"] or row["small_code"])

    for middle_id, label in middle_labels.items():
        middle_code = middle_id.rsplit(":", 1)[-1]
        graph.add_node(
            _node(
                middle_id,
                "middle",
                label,
                subtitle=f"중분류 {middle_code} · {middle_units[middle_id]:,}개 능력단위",
                layer=1,
                source_table="classifications",
                source_key=f"{major_code}:{middle_code}",
                properties={
                    "major_code": major_code,
                    "middle_code": middle_code,
                    "unit_count": middle_units[middle_id],
                },
            )
        )
        graph.add_edge(
            _edge(
                major_id,
                middle_id,
                "contains_middle",
                "중분류",
                source_table="classifications",
                source_key=f"{major_code}:{middle_code}",
            )
        )

    for small_id, label in small_labels.items():
        _, small_major, middle_code, small_code = small_id.split(":", 3)
        middle_id = f"middle:{small_major}:{middle_code}"
        graph.add_node(
            _node(
                small_id,
                "small",
                label,
                subtitle=f"소분류 {small_code} · {small_units[small_id]:,}개 능력단위",
                layer=2,
                source_table="classifications",
                source_key=f"{small_major}:{middle_code}:{small_code}",
                properties={
                    "major_code": small_major,
                    "middle_code": middle_code,
                    "small_code": small_code,
                    "unit_count": small_units[small_id],
                },
            )
        )
        graph.add_edge(
            _edge(
                middle_id,
                small_id,
                "contains_small",
                "소분류",
                source_table="classifications",
                source_key=f"{small_major}:{middle_code}:{small_code}",
            )
        )

    for row in rows:
        classification_id = int(row["classification_id"])
        scope_id = f"classification:{classification_id}"
        small_id = f"small:{major_code}:{row['middle_code']}:{row['small_code']}"
        graph.add_node(
            _node(
                scope_id,
                "classification",
                row["sub_name"],
                subtitle=f"세분류 {row['sub_code']} · {int(row['unit_count'] or 0):,}개 능력단위",
                layer=3,
                source_table="classifications",
                source_key=classification_id,
                properties={
                    "classification_id": classification_id,
                    "major_code": major_code,
                    "middle_code": row["middle_code"],
                    "small_code": row["small_code"],
                    "sub_code": row["sub_code"],
                    "unit_count": int(row["unit_count"] or 0),
                    "expandable": True,
                },
            )
        )
        graph.add_edge(
            _edge(
                small_id,
                scope_id,
                "contains_classification",
                "세분류",
                source_table="classifications",
                source_key=classification_id,
            )
        )

    return _graph_response(
        db_path,
        graph,
        mode="major_overview",
        query={"major_code": major_code, "resolved_by": "major_code"},
        focus={
            "node_id": major_id,
            "label": f"{major_code} {major_name}",
            "major_code": major_code,
            "hierarchy_label": f"{len(rows):,}개 세분류 · {total_units:,}개 능력단위",
        },
        warnings=[
            "대분류의 중·소·세분류 전체를 표시합니다. 세분류 노드를 선택하면 능력단위 목록을 엽니다.",
            "능력단위 상세에서 과업·KSA·훈련과정 근거를 단계적으로 확인할 수 있습니다.",
        ],
    )


def _search_units_for_classification(
    conn: sqlite3.Connection,
    classification_id: int,
) -> tuple[list[sqlite3.Row], str]:
    rows = conn.execute(
        """
        SELECT
            cu.unit_code,
            COALESCE(NULLIF(TRIM(cu.unit_name_refined), ''), cu.unit_name_raw) AS unit_name,
            COALESCE(NULLIF(TRIM(cu.api_unit_level), ''), cu.unit_level_raw) AS unit_level,
            cu.review_status AS unit_review_status,
            c.classification_id,
            c.major_code, c.major_name,
            c.middle_code, c.middle_name,
            c.small_code, c.small_name,
            c.sub_code, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE c.classification_id = ?
        ORDER BY cu.unit_code
        LIMIT 80
        """,
        (classification_id,),
    ).fetchall()
    return rows, "classification_id"


def build_ncs_knowledge_graph(
    db_path: str | Path,
    *,
    query: str = "",
    unit_code: str = "",
    major_code: str = "",
    classification_id: int | str | None = None,
    max_nodes: int | str | None = None,
) -> dict[str, Any]:
    """Build the all-NCS overview or a bounded unit evidence subgraph read-only."""

    query = _clean_text(query, limit=120)
    unit_code = _clean_text(unit_code, limit=80)
    major_code = _clean_text(major_code, limit=8)
    classification_text = _clean_text(classification_id, limit=24)
    node_limit = _clamp_node_limit(max_nodes)
    conn = _connect_readonly(db_path)
    try:
        tables = _table_names(conn)
        missing = [table for table in CORE_TABLES if table not in tables]
        if missing:
            raise KnowledgeGraphDataError(
                "schema_incomplete",
                "The database is missing tables required for the NCS knowledge graph.",
                missing_tables=missing,
            )

        if not query and not unit_code and not major_code and not classification_text:
            return _build_ncs_overview(conn, db_path)
        if major_code and not query and not unit_code and not classification_text:
            return _build_major_overview(
                conn,
                db_path,
                major_code=major_code,
            )

        if classification_text and not query and not unit_code:
            try:
                parsed_classification_id = int(classification_text)
            except ValueError as exc:
                raise KnowledgeGraphDataError(
                    "classification_invalid",
                    "NCS 세분류 식별자는 정수여야 합니다.",
                ) from exc
            matches, resolved_by = _search_units_for_classification(
                conn,
                parsed_classification_id,
            )
        else:
            matches, resolved_by = _search_units(
                conn,
                query=query,
                unit_code=unit_code,
            )
        if not matches:
            return {
                "ok": False,
                "schema": KNOWLEDGE_GRAPH_SCHEMA,
                "error": "unit_not_found",
                "detail": "일치하는 NCS 능력단위를 찾지 못했습니다.",
                "query": {
                    "text": query,
                    "unit_code": unit_code,
                    "major_code": major_code,
                    "classification_id": classification_text,
                },
                "candidates": [],
                "read_only": True,
                "db_writes": False,
                "approval_claim": False,
            }

        candidates = [_unit_payload(row) for row in matches]
        if not unit_code and (query or classification_text) and len(matches) > 1:
            return {
                "ok": True,
                "schema": KNOWLEDGE_GRAPH_SCHEMA,
                "mode": "unit_selection",
                "selection_required": True,
                "query": {
                    "text": query,
                    "major_code": major_code,
                    "classification_id": classification_text,
                    "resolved_by": resolved_by,
                },
                "candidates": candidates,
                "nodes": [],
                "edges": [],
                "read_only": True,
                "db_writes": False,
                "approval_claim": False,
                "provenance_policy": {
                    "local_ncs_sqlite_only": True,
                    "external_content_used": False,
                    "source_payload_exposed": False,
                },
            }

        root = matches[0]
        root_unit = _unit_payload(root)
        graph = _GraphBuilder(max_nodes=node_limit)

        scope_id = f"scope:{root['classification_id']}"
        unit_id = f"unit:{root['unit_code']}"
        graph.add_node(
            _node(
                scope_id,
                "scope",
                root["sub_name"],
                subtitle=root_unit["hierarchy_label"],
                layer=0,
                source_table="classifications",
                source_key=root["classification_id"],
                properties={
                    "major_code": root["major_code"],
                    "middle_code": root["middle_code"],
                    "small_code": root["small_code"],
                    "sub_code": root["sub_code"],
                    "hierarchy": root_unit["hierarchy"],
                },
            )
        )
        graph.add_node(
            _node(
                unit_id,
                "unit",
                root["unit_name"],
                subtitle=f"{root['unit_code']} · 수준 {root['unit_level']}",
                layer=1,
                source_table="competency_units",
                source_key=root["unit_code"],
                properties={
                    "unit_code": root["unit_code"],
                    "level": root["unit_level"],
                    "hierarchy": root_unit["hierarchy"],
                },
                review_state=root["unit_review_status"],
            )
        )
        graph.add_edge(
            _edge(
                scope_id,
                unit_id,
                "contains_unit",
                "분류에 포함",
                source_table="competency_units",
                source_key=root["unit_code"],
            )
        )

        element_rows, element_total = _fetch_elements(
            conn,
            str(root["unit_code"]),
            limit=8,
        )
        graph.note_omitted("element", max(element_total - len(element_rows), 0))
        element_ids: list[int] = []
        for row in element_rows:
            element_id = int(row["element_id"])
            element_ids.append(element_id)
            node_id = f"element:{element_id}"
            graph.add_node(
                _node(
                    node_id,
                    "element",
                    row["element_name"],
                    subtitle=f"요소 {row['element_no']} · 수준 {row['element_level']}",
                    layer=2,
                    source_table="competency_elements",
                    source_key=element_id,
                    properties={
                        "element_no": row["element_no"],
                        "element_code": row["element_code_raw"],
                        "level": row["element_level"],
                    },
                    review_state=row["review_status"],
                )
            )
            graph.add_edge(
                _edge(
                    unit_id,
                    node_id,
                    "contains_element",
                    "요소로 구성",
                    source_table="competency_elements",
                    source_key=element_id,
                )
            )

        task_rows, task_total = _fetch_tasks(conn, element_ids)
        graph.note_omitted("task", max(task_total - len(task_rows), 0))
        task_ids: list[int] = []
        for row in task_rows:
            task_id = int(row["criteria_id"])
            task_ids.append(task_id)
            node_id = f"task:{task_id}"
            graph.add_node(
                _node(
                    node_id,
                    "task",
                    row["criteria_text"],
                    subtitle=f"수행준거 {row['criteria_no']}",
                    layer=3,
                    source_table="performance_criteria",
                    source_key=task_id,
                    properties={
                        "criteria_no": row["criteria_no"],
                        "raw_text": _clean_text(row["criteria_text_raw"], limit=320),
                    },
                    review_state=row["review_status"],
                )
            )
            graph.add_edge(
                _edge(
                    f"element:{row['element_id']}",
                    node_id,
                    "defines_task",
                    "수행준거",
                    source_table="performance_criteria",
                    source_key=task_id,
                )
            )

        concept_rows, concept_total = _fetch_concepts_for_tasks(
            conn,
            task_ids,
            limit=28,
        )
        graph.note_omitted("concept", max(concept_total - len(concept_rows), 0))
        concept_ids: list[int] = []
        for row in concept_rows:
            concept_id = int(row["concept_id"])
            concept_ids.append(concept_id)
            concept_type = _concept_type(row["concept_type"])
            definition = ""
            if (
                row["definition_status"] == "defined"
                and row["review_status"] in TRUSTED_REVIEW_STATUSES
            ):
                definition = _clean_text(row["definition"], limit=320)
            graph.add_node(
                _node(
                    f"concept:{concept_id}",
                    concept_type,
                    row["concept_name"],
                    subtitle={
                        "knowledge": "지식 K",
                        "skill": "기술 S",
                        "attitude": "태도 A",
                    }.get(concept_type, "KSA 개념"),
                    layer=4,
                    source_table="ontology_concepts",
                    source_key=concept_id,
                    properties={
                        "concept_type": concept_type,
                        "task_evidence_count": int(row["task_count"] or 0),
                        "definition": definition,
                        "definition_state": (
                            "human_reviewed"
                            if definition
                            else "definition_unreviewed_or_missing"
                        ),
                    },
                    review_state=row["review_status"],
                )
            )

        if task_ids and concept_ids:
            task_placeholders = ",".join("?" for _ in task_ids)
            concept_placeholders = ",".join("?" for _ in concept_ids)
            link_rows = conn.execute(
                f"""
                SELECT link_id, criteria_id, concept_id, relation_type, link_status
                FROM criteria_concept_links
                WHERE criteria_id IN ({task_placeholders})
                  AND concept_id IN ({concept_placeholders})
                  AND LOWER(COALESCE(link_status, '')) <> 'rejected'
                ORDER BY criteria_id, concept_id
                """,
                [*task_ids, *concept_ids],
            ).fetchall()
            graph.note_omitted(
                "edge:task_requires_concept",
                max(len(link_rows) - 96, 0),
            )
            for row in link_rows[:96]:
                graph.add_edge(
                    _edge(
                        f"task:{row['criteria_id']}",
                        f"concept:{row['concept_id']}",
                        "task_requires_concept",
                        "과업 수행에 필요",
                        source_table="criteria_concept_links",
                        source_key=row["link_id"],
                        strength="direct_structure",
                        review_state=row["link_status"],
                        evidence=row["relation_type"],
                    )
                )

        _add_concept_relations(
            conn,
            graph,
            tables=tables,
            concept_ids=concept_ids,
            task_ids=task_ids,
        )
        _add_courses(
            conn,
            graph,
            tables=tables,
            unit_code=str(root["unit_code"]),
            concept_ids=concept_ids,
        )
        _add_supporting_evidence(
            conn,
            graph,
            tables=tables,
            unit_code=str(root["unit_code"]),
        )

        node_types = Counter(node["type"] for node in graph.nodes.values())
        edge_types = Counter(edge["type"] for edge in graph.edges.values())
        stat = Path(db_path).stat()
        return {
            "ok": True,
            "schema": KNOWLEDGE_GRAPH_SCHEMA,
            "mode": "unit_detail",
            "selection_required": False,
            "query": {
                "text": query,
                "unit_code": unit_code,
                "resolved_by": resolved_by,
            },
            "focus": {
                "node_id": unit_id,
                **root_unit,
            },
            "candidates": candidates,
            "nodes": list(graph.nodes.values()),
            "edges": list(graph.edges.values()),
            "facets": {
                "node_types": dict(sorted(node_types.items())),
                "edge_types": dict(sorted(edge_types.items())),
            },
            "truncation": {
                "truncated": bool(graph.omitted),
                "node_limit": graph.max_nodes,
                "edge_limit": graph.max_edges,
                "omitted_by_type": dict(sorted(graph.omitted.items())),
            },
            "data_version": {
                "db_modified_at": datetime.fromtimestamp(
                    stat.st_mtime,
                    tz=timezone.utc,
                ).isoformat(),
                "contract": KNOWLEDGE_GRAPH_SCHEMA,
            },
            "read_only": True,
            "db_writes": False,
            "approval_claim": False,
            "active_product_scope": "NCS",
            "audit": {
                "read_only": True,
                "sqf_used": False,
                "learning_modules_used": False,
                "framework_reference_scored": False,
            },
            "provenance_policy": {
                "local_ncs_sqlite_only": True,
                "external_content_used": False,
                "source_payload_exposed": False,
                "trusted_definition_required_for_display": True,
            },
            "warnings": [
                "그래프 관계는 교육훈련 탐색 근거이며 공식 자격·법적 적격성 판단이 아닙니다.",
                "자동·후보 관계는 사람의 승인 상태로 해석하지 않습니다.",
                "검토되지 않은 boilerplate 또는 draft 정의는 표시하지 않습니다.",
            ],
        }
    finally:
        conn.close()
