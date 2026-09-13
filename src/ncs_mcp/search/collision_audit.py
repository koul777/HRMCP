"""Read-only inventory of ambiguous labels in the NCS search corpus.

This module deliberately operates only on the three official source tables used
for scope resolution: ``classifications``, ``competency_units`` and
``competency_elements``.  It does not read evaluation holdouts, create aliases,
or write status/review fields.  The output is a diagnostic inventory and a set
of machine-readable *candidate* containment scenarios; it is not a relevance
judgement or an approval signal.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA = "ncs_scope_collision_audit_v2"
SOURCE_TABLES = ("classifications", "competency_units", "competency_elements")
CLASSIFICATION_FIELDS = ("major", "middle", "small", "sub")
CLASSIFICATION_LEVELS = tuple(
    (level, f"{level}_code", f"{level}_name") for level in CLASSIFICATION_FIELDS
)
DEFAULT_COLLISION_CAP = 500
DEFAULT_PATHS_PER_COLLISION = 20
DEFAULT_HAZARD_CAP = 1000
DEFAULT_SCENARIO_LIMIT = 1000
DEFAULT_SCOPE_LABEL_CAP = 5000


def normalize_label(value: Any) -> str:
    """Return a conservative, deterministic label key.

    NFKC handles compatibility forms, casefold handles Latin case, and
    punctuation/separator runs become spaces.  No stemming, synonym expansion,
    suffix removal, or alias generation is performed: collisions represent
    exact normalized source labels only.
    """

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    chars = [char if char.isalnum() else " " for char in text]
    return " ".join("".join(chars).split())


def _open_read_only(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"NCS database does not exist: {path}")
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _text(row: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _classification_path(row: Mapping[str, Any]) -> dict[str, str]:
    path = {
        "major_code": str(row.get("major_code") or ""),
        "major_name": str(row.get("major_name") or ""),
        "middle_code": str(row.get("middle_code") or ""),
        "middle_name": str(row.get("middle_name") or ""),
        "small_code": str(row.get("small_code") or ""),
        "small_name": str(row.get("small_name") or ""),
        "sub_code": str(row.get("sub_code") or ""),
        "sub_name": str(row.get("sub_name") or ""),
    }
    path["classification_full_path_key"] = _path_key(path)
    return path


def _path_key(path: Mapping[str, Any]) -> str:
    codes = [str(path.get(f"{level}_code") or "") for level in CLASSIFICATION_FIELDS]
    return "/".join(codes)


def _scope_path_key(path: Mapping[str, Any], level: str) -> str:
    """Return the code prefix represented by a classification level."""

    try:
        end = CLASSIFICATION_FIELDS.index(level) + 1
    except ValueError:
        return ""
    return "/".join(str(path.get(f"{item}_code") or "") for item in CLASSIFICATION_FIELDS[:end])


def _row_dicts(conn: sqlite3.Connection, table: str) -> Iterable[dict[str, Any]]:
    cursor = conn.execute(f"SELECT * FROM {table}")
    names = [description[0] for description in cursor.description or ()]
    for row in cursor:
        yield dict(zip(names, row))


def _record(
    *,
    record_type: str,
    record_id: str,
    label: str,
    label_field: str,
    path: Mapping[str, Any],
    source_table: str,
    source_pk: Any,
) -> dict[str, Any] | None:
    normalized = normalize_label(label)
    if not normalized:
        return None
    path_copy = dict(path)
    return {
        "record_type": record_type,
        "record_id": str(record_id),
        "label": label,
        "normalized_label": normalized,
        "label_field": label_field,
        "source_table": source_table,
        "source_pk": str(source_pk),
        "path_key": str(path_copy.get("path_key") or _path_key(path_copy)),
        "scope_level": path_copy.get("scope_level", ""),
        "scope_path_key": path_copy.get("scope_path_key", ""),
        "path": path_copy,
    }


def _load_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load source labels while preserving enough path data for containment."""

    records: list[dict[str, Any]] = []
    classification_records: dict[str, dict[str, Any]] = {}
    classification_paths: dict[str, dict[str, Any]] = {}
    for row in _row_dicts(conn, "classifications"):
        path = _classification_path(row)
        classification_id = row.get("classification_id")
        classification_paths[str(classification_id)] = path
        for level, code_field, name_field in CLASSIFICATION_LEVELS:
            label = _text(row, name_field)
            if not label or not _text(row, code_field):
                continue
            scoped_path = dict(path)
            scoped_path.update(
                {
                    "scope_level": level,
                    "scope_code": _text(row, code_field),
                    "scope_name": label,
                    "scope_path_key": _scope_path_key(path, level),
                    "scope_node_key": f"{level}:{_scope_path_key(path, level)}",
                    "path_key": _scope_path_key(path, level),
                }
            )
            item = _record(
                record_type="classification",
                record_id=f"classification:{scoped_path['scope_node_key']}",
                label=label,
                label_field=name_field,
                path=scoped_path,
                source_table="classifications",
                source_pk=classification_id,
            )
            if item:
                classification_records.setdefault(str(scoped_path["scope_node_key"]), item)
    records.extend(classification_records.values())

    unit_paths: dict[str, dict[str, Any]] = {}
    for row in _row_dicts(conn, "competency_units"):
        classification_id = row.get("classification_id")
        path = classification_paths.get(str(classification_id), {}) if classification_id is not None else {}
        path = dict(path)
        path["unit_code"] = str(row.get("unit_code") or "")
        unit_code = str(row.get("unit_code") or "")
        unit_paths[unit_code] = path
        label = _text(row, "unit_name_raw")
        item = _record(
            record_type="competency_unit",
            record_id=unit_code,
            label=label,
            label_field="unit_name_raw",
            path=path,
            source_table="competency_units",
            source_pk=unit_code,
        )
        if item:
            records.append(item)

    for row in _row_dicts(conn, "competency_elements"):
        unit_code = str(row.get("unit_code") or "")
        path = dict(unit_paths.get(unit_code, {}))
        path["element_id"] = str(row.get("element_id") or "")
        label = _text(row, "element_name_raw")
        item = _record(
            record_type="competency_element",
            record_id=str(row.get("element_id")),
            label=label,
            label_field="element_name_raw",
            path=path,
            source_table="competency_elements",
            source_pk=row.get("element_id"),
        )
        if item:
            records.append(item)
    return records


def _collision_group_key(record: Mapping[str, Any]) -> str:
    normalized = str(record["normalized_label"])
    if record.get("record_type") == "classification":
        return f"classification:{normalized}"
    return f"leaf:{normalized}"


def _path_parts(value: Any) -> tuple[str, ...]:
    return tuple(part for part in str(value or "").split("/") if part)


def _scope_paths_compatible(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_parts = _path_parts(left.get("scope_path_key"))
    right_parts = _path_parts(right.get("scope_path_key"))
    if not left_parts or not right_parts:
        return False
    shortest = min(len(left_parts), len(right_parts))
    return left_parts[:shortest] == right_parts[:shortest]


def _canonical_classification_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse same-branch ancestor labels to their deepest compatible node.

    Classification labels are level-agnostic for collision purposes, but a
    repeated ancestor label on the same branch is not ambiguous.  Distinct
    branches retain separate records and therefore remain observable as a
    collision candidate.
    """

    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record["record_type"] == "classification":
            by_label[record["normalized_label"]].append(record)
    retained: list[dict[str, Any]] = []
    for label_records in by_label.values():
        ordered = sorted(
            label_records,
            key=lambda item: (-len(_path_parts(item.get("scope_path_key"))), item["record_id"]),
        )
        label_retained: list[dict[str, Any]] = []
        for record in ordered:
            if any(_scope_paths_compatible(record, existing) for existing in label_retained):
                continue
            label_retained.append(record)
        retained.extend(label_retained)
    return retained


def _scope_filter(path: Mapping[str, Any], level: str | None = None) -> dict[str, str]:
    levels = CLASSIFICATION_FIELDS
    if level in levels:
        levels = levels[: levels.index(level) + 1]
    return {
        f"{item}_code": str(path.get(f"{item}_code") or "")
        for item in levels
        if path.get(f"{item}_code")
    }


def _collision_id(normalized_label: str, group_key: str = "") -> str:
    digest = hashlib.sha256(f"{group_key}|{normalized_label}".encode("utf-8")).hexdigest()[:16]
    return f"collision-{digest}"


def _collision_rows(
    records: Iterable[dict[str, Any]],
    *,
    paths_per_collision: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[_collision_group_key(record)].append(record)
    collisions: list[dict[str, Any]] = []
    for group_key, items in grouped.items():
        normalized = str(items[0]["normalized_label"])
        path_keys = sorted({str(item["path_key"]) for item in items})
        record_types = sorted({str(item["record_type"]) for item in items})
        if len(items) < 2 and len(path_keys) < 2:
            continue
        type_counts = Counter(str(item["record_type"]) for item in items)
        labels = sorted({str(item["label"]) for item in items})
        paths = []
        for path_key in path_keys:
            path_items = [item for item in items if item["path_key"] == path_key]
            sample = path_items[0]
            if len(paths) >= paths_per_collision:
                continue
            paths.append(
                {
                    "path_key": path_key,
                    "path": sample["path"],
                    "record_count": len(path_items),
                    "record_type_counts": dict(sorted(Counter(i["record_type"] for i in path_items).items())),
                    "record_ids_sample": sorted(i["record_id"] for i in path_items)[:10],
                }
            )
        collisions.append(
            {
                "collision_id": _collision_id(normalized, group_key),
                "collision_group": group_key,
                "normalized_label": normalized,
                "source_labels": labels,
                "record_count": len(items),
                "distinct_path_count": len(path_keys),
                "record_type_counts": dict(sorted(type_counts.items())),
                "record_types": record_types,
                "cross_type": len(record_types) > 1,
                "cross_path": len(path_keys) > 1,
                "collision_stratum": (
                    "classification_cross_path"
                    if group_key.startswith("classification:") and len(path_keys) > 1
                    else "cross_type"
                    if len(record_types) > 1
                    else "leaf"
                ),
                "paths": paths,
                "paths_truncated": len(path_keys) > paths_per_collision,
                "_all_path_keys": path_keys,
            }
        )
    return sorted(
        collisions,
        key=lambda item: (-item["distinct_path_count"], -item["record_count"], item["normalized_label"]),
    )


def _collision_major_key(collision: Mapping[str, Any]) -> str:
    paths = collision.get("paths") or []
    if paths:
        return str((paths[0].get("path") or {}).get("major_code") or "unknown")
    return "unknown"


def _stratified_collision_select(
    collisions: list[dict[str, Any]], cap: int
) -> list[dict[str, Any]]:
    """Select collision representatives fairly across risk strata and majors."""

    if cap <= 0:
        return []
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for collision in collisions:
        buckets[(str(collision["collision_stratum"]), _collision_major_key(collision))].append(collision)
    for items in buckets.values():
        items.sort(
            key=lambda item: (
                -int(item["distinct_path_count"] > 1),
                -int(item["cross_type"]),
                -item["distinct_path_count"],
                len(item["normalized_label"]),
                item["normalized_label"],
                item["collision_id"],
            )
        )
    selected: list[dict[str, Any]] = []
    bucket_keys = sorted(buckets)
    while len(selected) < cap and bucket_keys:
        progressed = False
        for bucket_key in bucket_keys:
            items = buckets[bucket_key]
            if not items:
                continue
            selected.append(items.pop(0))
            progressed = True
            if len(selected) >= cap:
                break
        if not progressed:
            break
    return selected


def _scenario_candidates(
    records: list[dict[str, Any]],
    collisions: list[dict[str, Any]],
    limit: int,
    *,
    paths_per_collision: int,
) -> list[dict[str, Any]]:
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_label[_collision_group_key(record)].append(record)
    candidates: list[dict[str, Any]] = []
    for collision in collisions:
        if not collision["cross_path"]:
            continue
        normalized = collision["normalized_label"]
        members = by_label[collision["collision_group"]]
        all_path_keys = collision.get("_all_path_keys", [])
        for path_key in all_path_keys:
            scoped = [item for item in members if item["path_key"] == path_key]
            if not scoped:
                continue
            path_meta = scoped[0]["path"]
            filters = _scope_filter(path_meta, scoped[0].get("scope_level") or None)
            candidates.append(
                {
                    "scenario_id": f"scope-collision-{hashlib.sha256((collision['collision_id'] + '|' + path_key).encode()).hexdigest()[:16]}",
                    "collision_id": collision["collision_id"],
                    "query": collision["source_labels"][0],
                    "normalized_label": normalized,
                    "scope_filter": filters,
                    "target_path_key": path_key,
                    "expected_containment": {
                        "must_include_path_key": path_key,
                        "must_exclude_path_keys": [
                            sibling for sibling in all_path_keys
                            if sibling != path_key
                        ][:paths_per_collision],
                        "sibling_path_count": max(0, len(all_path_keys) - 1),
                        "excluded_path_keys_truncated": len(all_path_keys) - 1 > paths_per_collision,
                        "is_candidate_only": True,
                    },
                    "expected_record_ids": {
                        "classifications": sorted(i["record_id"] for i in scoped if i["record_type"] == "classification"),
                        "competency_units": sorted(i["record_id"] for i in scoped if i["record_type"] == "competency_unit"),
                        "competency_elements": sorted(i["record_id"] for i in scoped if i["record_type"] == "competency_element"),
                    },
                    "source": "official_ncs_source_tables",
                }
            )
            if len(candidates) >= limit:
                return candidates
    return candidates


def _compact_label(value: Any) -> str:
    return normalize_label(value).replace(" ", "")


def _leaf_is_under_scope(
    leaf_path: Mapping[str, Any], scope_path: Mapping[str, Any], level: str
) -> bool:
    try:
        end = CLASSIFICATION_FIELDS.index(level) + 1
    except ValueError:
        return False
    return all(
        str(leaf_path.get(f"{item}_code") or "")
        == str(scope_path.get(f"{item}_code") or "")
        for item in CLASSIFICATION_FIELDS[:end]
    )


def _stratified_hazard_select(
    hazards: list[dict[str, Any]], cap: int
) -> list[dict[str, Any]]:
    """Select lexical hazards across kind, scope level, and major buckets."""

    if cap <= 0:
        return []
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for hazard in hazards:
        scope = hazard["classification_scope_label"]
        scope_filter = scope.get("scope_filter") or {}
        bucket = (
            str(hazard["hazard_kind"]),
            str(scope.get("level") or "unknown"),
            str(scope_filter.get("major_code") or "unknown"),
        )
        buckets[bucket].append(hazard)
    for items in buckets.values():
        items.sort(
            key=lambda item: (
                len(item["classification_scope_label"]["normalized_label"].replace(" ", "")),
                len(item["off_path_leaf"]["normalized_label"].replace(" ", "")),
                item["classification_scope_label"]["normalized_label"],
                item["off_path_leaf"]["normalized_label"],
                item["hazard_id"],
            )
        )
    selected: list[dict[str, Any]] = []
    # Reserve one representative of each primary lexical class when present;
    # the remainder is filled by kind × level × major round-robin.
    scope_frequency = Counter(
        item["classification_scope_label"]["record_id"] for item in hazards
    )
    for kind in ("prefix", "internal_compound"):
        candidates = [item for item in hazards if item["hazard_kind"] == kind]
        if candidates and len(selected) < cap:
            selected.append(min(candidates, key=lambda item: (
                -scope_frequency[item["classification_scope_label"]["record_id"]],
                len(item["classification_scope_label"]["normalized_label"].replace(" ", "")),
                len(item["off_path_leaf"]["normalized_label"].replace(" ", "")),
                item["classification_scope_label"]["normalized_label"],
                item["off_path_leaf"]["normalized_label"],
                item["hazard_id"],
            )))
    if len(selected) < cap and any(item["hazard_kind"] == "exact_off_path" for item in hazards):
        exact_candidates = [item for item in hazards if item["hazard_kind"] == "exact_off_path"]
        selected.append(min(exact_candidates, key=lambda item: (
            item["classification_scope_label"]["normalized_label"],
            item["off_path_leaf"]["normalized_label"],
            item["hazard_id"],
        )))
    selected_ids = {item["hazard_id"] for item in selected}
    bucket_keys = sorted(buckets)
    while len(selected) < cap and bucket_keys:
        progressed = False
        for bucket_key in bucket_keys:
            items = buckets[bucket_key]
            while items and items[0]["hazard_id"] in selected_ids:
                items.pop(0)
            if not items:
                continue
            selected.append(items.pop(0))
            selected_ids.add(selected[-1]["hazard_id"])
            progressed = True
            if len(selected) >= cap:
                break
        if not progressed:
            break
    return selected


def _scope_hazards(
    records: list[dict[str, Any]],
    *,
    hazard_cap: int,
    scenario_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Find source-derived off-path prefix and internal-compound hazards.

    This intentionally does not infer aliases or deny-lists.  A hazard means
    only that a leaf label contains a classification-scope label after
    conservative normalization; the candidate is for testing containment.
    """

    scope_records = [item for item in records if item["record_type"] == "classification"]
    leaf_records = [
        item for item in records if item["record_type"] in {"competency_unit", "competency_element"}
    ]
    hazards: list[dict[str, Any]] = []
    scope_by_compact: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for scope in scope_records:
        scope_compact = _compact_label(scope["normalized_label"])
        if scope.get("scope_level") and scope_compact:
            scope_by_compact[scope_compact].append(scope)
    scope_lengths = sorted({len(value) for value in scope_by_compact})
    # Index identical leaf text/path pairs first.  Substring probes then look up
    # only scope labels that can actually occur, rather than a Cartesian scan.
    leaf_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for leaf in leaf_records:
        compact = _compact_label(leaf["normalized_label"])
        if compact:
            leaf_groups[(compact, leaf["path_key"])].append(leaf)
    for (leaf_compact, _leaf_path_key), grouped_leaves in leaf_groups.items():
        possible_scopes: set[str] = set()
        for length in scope_lengths:
            if length > len(leaf_compact):
                continue
            possible_scopes.update(
                leaf_compact[index : index + length]
                for index in range(len(leaf_compact) - length + 1)
                if leaf_compact[index : index + length] in scope_by_compact
            )
        for scope_compact in sorted(possible_scopes):
            for scope in scope_by_compact[scope_compact]:
                scope_level = str(scope["scope_level"])
                for leaf in grouped_leaves:
                    if _leaf_is_under_scope(leaf["path"], scope["path"], scope_level):
                        continue
                    if leaf_compact == scope_compact:
                        hazard_kind = "exact_off_path"
                    elif leaf_compact.startswith(scope_compact):
                        hazard_kind = "prefix"
                    else:
                        hazard_kind = "internal_compound"
                    hazards.append(
                {
                    "hazard_id": "scope-hazard-" + hashlib.sha256(
                        f"{scope['record_id']}|{leaf['record_id']}|{hazard_kind}".encode("utf-8")
                    ).hexdigest()[:16],
                    "hazard_kind": hazard_kind,
                    "classification_scope_label": {
                        "record_id": scope["record_id"],
                        "level": scope_level,
                        "label": scope["label"],
                        "normalized_label": scope["normalized_label"],
                        "scope_path_key": scope["scope_path_key"],
                        "scope_filter": _scope_filter(scope["path"], scope_level),
                    },
                    "off_path_leaf": {
                        "record_type": leaf["record_type"],
                        "record_id": leaf["record_id"],
                        "label": leaf["label"],
                        "normalized_label": leaf["normalized_label"],
                        "path_key": leaf["path_key"],
                    },
                    "source": "official_ncs_source_tables",
                    "candidate_only": True,
                    }
                    )
    hazard_kind_order = {"prefix": 0, "internal_compound": 1, "exact_off_path": 2}
    hazards.sort(key=lambda item: (hazard_kind_order[item["hazard_kind"]], item["classification_scope_label"]["normalized_label"], item["off_path_leaf"]["normalized_label"], item["hazard_id"]))
    totals = {
        "off_path_hazard_total_count": len(hazards),
        "off_path_prefix_hazard_count": sum(item["hazard_kind"] == "prefix" for item in hazards),
        "off_path_internal_compound_hazard_count": sum(item["hazard_kind"] == "internal_compound" for item in hazards),
        "off_path_exact_hazard_count": sum(item["hazard_kind"] == "exact_off_path" for item in hazards),
    }
    serialized_hazards = _stratified_hazard_select(hazards, hazard_cap)
    scenarios: list[dict[str, Any]] = []
    for hazard in (serialized_hazards if scenario_limit > 0 else []):
        scope = hazard["classification_scope_label"]
        leaf = hazard["off_path_leaf"]
        scenarios.append(
            {
                "scenario_id": "scope-hazard-scenario-" + hazard["hazard_id"].removeprefix("scope-hazard-"),
                "hazard_id": hazard["hazard_id"],
                "query": scope["label"],
                "normalized_query": scope["normalized_label"],
                "scope_filter": scope["scope_filter"],
                "hazard_kind": hazard["hazard_kind"],
                "off_path_leaf": leaf,
                "expected_behavior": {
                    "candidate_only": True,
                    "off_path_leaf_excluded_when_scope_is_applied": True,
                    "bare_unscoped_search_may_return_source_row": True,
                    "scope_path_key": scope["scope_path_key"],
                    "off_path_leaf_path_key": leaf["path_key"],
                },
                "source": "official_ncs_source_tables",
            }
        )
        if len(scenarios) >= scenario_limit:
            break
    totals["serialized_hazard_count"] = len(serialized_hazards)
    totals["hazard_serialization_truncated"] = int(len(hazards) > hazard_cap)
    totals["hazard_scenario_candidate_count"] = len(hazards)
    totals["serialized_hazard_scenario_candidate_count"] = len(scenarios)
    totals["hazard_scenario_serialization_truncated"] = int(len(hazards) > scenario_limit)
    return serialized_hazards, scenarios, totals


def build_collision_inventory(
    db_path: Path | str,
    *,
    top_n: int = 100,
    scenario_limit: int = DEFAULT_SCENARIO_LIMIT,
    collision_cap: int = DEFAULT_COLLISION_CAP,
    paths_per_collision: int = DEFAULT_PATHS_PER_COLLISION,
    hazard_cap: int = DEFAULT_HAZARD_CAP,
    scope_label_cap: int = DEFAULT_SCOPE_LABEL_CAP,
) -> dict[str, Any]:
    """Build a full all-major inventory using a SQLite read-only connection."""

    if top_n <= 0 or scenario_limit < 0 or collision_cap <= 0 or paths_per_collision <= 0 or hazard_cap < 0 or scope_label_cap < 0:
        raise ValueError("top_n/collision_cap/paths_per_collision must be positive; other caps cannot be negative")
    conn = _open_read_only(db_path)
    try:
        missing = [table for table in SOURCE_TABLES if not _table_columns(conn, table)]
        if missing:
            raise ValueError("database is missing required source tables: " + ", ".join(missing))
        records = _load_records(conn)
    finally:
        conn.close()

    all_labels = Counter(record["normalized_label"] for record in records)
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_type[record["record_type"]].append(record)
    canonical_classifications = _canonical_classification_records(
        by_type["classification"]
    )
    collision_records = canonical_classifications + by_type["competency_unit"] + by_type["competency_element"]
    all_collisions = _collision_rows(collision_records, paths_per_collision=paths_per_collision)
    collisions = _stratified_collision_select(all_collisions, collision_cap)
    cross_path = [item for item in all_collisions if item["cross_path"]]
    cross_type = [item for item in all_collisions if item["cross_type"]]
    scenarios = _scenario_candidates(
        collision_records,
        all_collisions,
        scenario_limit,
        paths_per_collision=paths_per_collision,
    )
    containment_scenario_total_count = sum(
        len(item.get("_all_path_keys", [])) for item in all_collisions if item["cross_path"]
    )
    serialized_hazards, hazard_scenarios, hazard_metrics = _scope_hazards(
        records,
        hazard_cap=hazard_cap,
        scenario_limit=scenario_limit,
    )
    type_metrics: dict[str, dict[str, Any]] = {}
    for record_type in ("classification", "competency_unit", "competency_element"):
        typed = by_type[record_type]
        labels = Counter(item["normalized_label"] for item in typed)
        type_collisions = [item for item in all_collisions if record_type in item["record_types"]]
        type_metrics[record_type] = {
            "source_table": {"classification": "classifications", "competency_unit": "competency_units", "competency_element": "competency_elements"}[record_type],
            "record_count": len(typed),
            "normalized_label_count": len(labels),
            "duplicate_normalized_label_count": sum(1 for count in labels.values() if count > 1),
            "cross_path_collision_count": sum(1 for item in type_collisions if item["cross_path"]),
            "distinct_path_count": len({item["path_key"] for item in typed}),
            "distinct_node_count": len({item["record_id"] for item in typed}),
        }

    metrics = {
        "record_count": len(records),
        "normalized_label_count": len(all_labels),
        "collision_label_count": len(all_collisions),
        "collision_label_total_count": len(all_collisions),
        "serialized_collision_label_count": len(collisions),
        "collision_serialization_truncated": int(len(all_collisions) > collision_cap),
        "cross_path_collision_label_count": len(cross_path),
        "cross_type_collision_label_count": len(cross_type),
        "ambiguous_record_count": sum(item["record_count"] for item in all_collisions),
        "classification_path_count": len({
            item["path"].get("classification_full_path_key")
            for item in records
            if item["path"].get("classification_full_path_key")
        }),
        "all_record_path_count": len({
            item["path"].get("classification_full_path_key") or item["path_key"]
            for item in records
        }),
        "containment_scenario_candidate_count": containment_scenario_total_count,
        "serialized_containment_scenario_candidate_count": len(scenarios),
        "containment_scenario_serialization_truncated": int(containment_scenario_total_count > scenario_limit),
        **hazard_metrics,
    }
    classification_scope_labels = [
        item for item in records if item["record_type"] == "classification"
    ]
    metrics.update(
        {
            "classification_scope_label_total_count": len(classification_scope_labels),
            "serialized_classification_scope_label_count": min(
                scope_label_cap, len(classification_scope_labels)
            ),
            "classification_scope_label_serialization_truncated": int(
                len(classification_scope_labels) > scope_label_cap
            ),
            "classification_level_record_counts": dict(
                sorted(
                    Counter(item["scope_level"] for item in classification_scope_labels).items()
                )
            ),
        }
    )
    classification_level_metrics = {}
    for level in CLASSIFICATION_FIELDS:
        level_records = [item for item in classification_scope_labels if item["scope_level"] == level]
        level_labels = Counter(item["normalized_label"] for item in level_records)
        classification_level_metrics[level] = {
            "node_count": len(level_records),
            "normalized_label_count": len(level_labels),
            "normalized_label_collision_count": sum(1 for count in level_labels.values() if count > 1),
        }
    major_codes = sorted(
        {
            str(item["path"].get("major_code") or "")
            for item in records
            if item["path"].get("major_code")
        }
    )
    all_major_scope_covered = bool(major_codes)
    source_table_counts = {
        item["source_table"]: item["record_count"] for item in type_metrics.values()
    }
    release_gate_inputs = {
        "all_major_scope_covered": all_major_scope_covered,
        "major_codes": major_codes,
        "major_count": len(major_codes),
        "source_table_counts": source_table_counts,
        "path_counts": {
            "classification_paths": metrics["classification_path_count"],
            "all_record_paths": metrics["all_record_path_count"],
        },
        "collision_counts": {
            "all": metrics["collision_label_count"],
            "cross_path": metrics["cross_path_collision_label_count"],
            "cross_type": metrics["cross_type_collision_label_count"],
        },
        "containment_scenario_candidate_count": metrics["containment_scenario_candidate_count"],
        "gate_is_observational": True,
    }
    serialized_collisions = []
    for collision in collisions:
        public_collision = dict(collision)
        public_collision.pop("_all_path_keys", None)
        serialized_collisions.append(public_collision)
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "scope_type": "all_ncs_source_tables",
            "major_scope": "all_major_codes",
            "major_codes": major_codes,
            "major_count": len(major_codes),
            "all_major_scope_covered": all_major_scope_covered,
            "resolution": "resolved" if all_major_scope_covered else "unresolved",
            "source_tables": list(SOURCE_TABLES),
            "read_only": True,
            "holdout_inspected": False,
            "aliases_generated": False,
            "deny_lists_generated": False,
            "human_review_statuses_written": False,
        },
        "metrics": metrics,
        "summary": metrics,
        "release_gate_inputs": release_gate_inputs,
        "per_type": type_metrics,
        "classification_level_metrics": classification_level_metrics,
        "limits": {
            "collision_cap": collision_cap,
            "paths_per_collision": paths_per_collision,
            "hazard_cap": hazard_cap,
            "scenario_limit": scenario_limit,
            "scope_label_cap": scope_label_cap,
            "top_n": top_n,
        },
        "top_collisions": serialized_collisions[: min(top_n, collision_cap)],
        "collisions": serialized_collisions,
        "classification_scope_labels": classification_scope_labels[:scope_label_cap],
        "scoped_containment_scenarios": scenarios,
        "scope_label_hazards": serialized_hazards,
        "scope_hazard_scenarios": hazard_scenarios,
        "scenario_contract": {
            "purpose": "regression candidate inputs for scoped containment only",
            "candidate_not_relevance_label": True,
            "required_runtime_behavior": "when scope_filter is applied, exclude the off-path leaf; bare unscoped lexical search may return the source row",
        },
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "# NCS Scope Collision Audit",
        "",
        f"- Schema: `{report['schema']}`",
        "- Scope: all NCS major codes; official source tables only",
        "- Database mode: read-only; holdout inspected: false",
        "- This is a collision inventory and containment-test candidate pack, not a relevance or approval judgement.",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key, value in metrics.items():
        lines.append(f"| `{key}` | {value} |")
    lines.extend(["", "## Per-type counts", "", "| Type | Records | Unique labels | Duplicate labels | Cross-path collisions | Paths |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
    for record_type, item in report["per_type"].items():
        lines.append(f"| `{record_type}` | {item['record_count']} | {item['normalized_label_count']} | {item['duplicate_normalized_label_count']} | {item['cross_path_collision_count']} | {item['distinct_path_count']} |")
    lines.extend(["", "## Top normalized-label collisions", "", "| Label | Records | Paths | Types | Example paths |", "| --- | ---: | ---: | --- | --- |"])
    for item in report["top_collisions"][:50]:
        paths = "; ".join(str(path["path_key"]) for path in item["paths"][:4])
        lines.append(f"| `{item['normalized_label']}` | {item['record_count']} | {item['distinct_path_count']} | {', '.join(item['record_types'])} | {paths} |")
    lines.extend(["", "## Scoped containment scenario candidates", "", f"Generated candidates: `{metrics['containment_scenario_candidate_count']}`. These inputs encode source-derived expected IDs/path exclusions and are not human relevance labels.", ""])
    for scenario in report["scoped_containment_scenarios"][:50]:
        lines.append(f"- `{scenario['scenario_id']}` query=`{scenario['query']}` target=`{scenario['target_path_key']}` filter=`{json.dumps(scenario['scope_filter'], ensure_ascii=False, sort_keys=True)}`")
    lines.extend(["", "## Off-path prefix/internal-compound hazards", "", f"Exact source-derived hazards: `{metrics['off_path_hazard_total_count']}`; serialized under the configured hazard cap: `{metrics['serialized_hazard_count']}`.", ""])
    for hazard in report.get("scope_label_hazards", [])[:50]:
        scope = hazard["classification_scope_label"]
        leaf = hazard["off_path_leaf"]
        lines.append(f"- `{hazard['hazard_kind']}` scope=`{scope['label']}` ({scope['level']}) → off-path `{leaf['label']}` [{leaf['record_type']}]")
    lines.extend(["", "## Serialization limits", "", f"`{json.dumps(report.get('limits', {}), ensure_ascii=False, sort_keys=True)}`", ""])
    lines.extend(["", "## Safety", "", "No source rows were changed. No aliases, deny-lists, or `human_reviewed`/`accepted`/`reviewed` statuses were generated or written.", ""])
    return "\n".join(lines)


def write_report(
    db_path: Path | str,
    json_path: Path | str,
    markdown_path: Path | str,
    *,
    top_n: int = 100,
    scenario_limit: int = DEFAULT_SCENARIO_LIMIT,
    collision_cap: int = DEFAULT_COLLISION_CAP,
    paths_per_collision: int = DEFAULT_PATHS_PER_COLLISION,
    hazard_cap: int = DEFAULT_HAZARD_CAP,
    scope_label_cap: int = DEFAULT_SCOPE_LABEL_CAP,
) -> dict[str, Any]:
    report = build_collision_inventory(
        db_path,
        top_n=top_n,
        scenario_limit=scenario_limit,
        collision_cap=collision_cap,
        paths_per_collision=paths_per_collision,
        hazard_cap=hazard_cap,
        scope_label_cap=scope_label_cap,
    )
    json_target = Path(json_path)
    markdown_target = Path(markdown_path)
    json_target.parent.mkdir(parents=True, exist_ok=True)
    markdown_target.parent.mkdir(parents=True, exist_ok=True)
    json_target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_target.write_text(render_markdown(report), encoding="utf-8")
    return report


__all__ = ["SCHEMA", "build_collision_inventory", "normalize_label", "render_markdown", "write_report"]
