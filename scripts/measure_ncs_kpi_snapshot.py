"""Measure the locally measurable parts of the NCS KPI v1 contract.

Report-only: opens the DB read-only, calls recommenders with save=False, makes
no network calls and writes nothing but the requested output files. It covers
K10 (census), a K03 structural diagnostic on the gold transition scenarios, G03
checks on the slots those scenarios return, the G01 source-KSA manifest
comparison, and a G10 scan of git-tracked files for .env secret values (counts
and paths only). G02 checks trusted review statuses against packet-backed
audit rows. On the same scenarios it traces the serving SQL for G04
(SQF/learning-module table access) and G08 (writes from save=False calls), and
runs the internal education planner for the G05 response contract and, with
one fixed delivery constraint profile, for the G06 fit checks.
Everything else stays unknown here.
A pass here is not human approval, release readiness or deployment evidence.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import random
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SCHEMA = "ncs_kpi_snapshot_v1"
SEED = 20261018
RESAMPLES = 10000
K03_SLOTS = 3
K03_LIMIT = 5
GIT_FLAGS = ["-c", "filter.lfs.clean=cat", "-c", "filter.lfs.smudge=cat",
             "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false"]
LFS_EXCLUDES = [":(exclude)data/processed/*.db", ":(exclude)data/processed/*.db-*", ":(exclude)data/raw/*.xlsx",
                ":(exclude)data/raw/*.xls", ":(exclude)data/ocr/tessdata/*.traineddata"]
LEGACY_TABLE_MARKERS = ("sqf", "learning_module")
FRAMEWORK_TABLE_PREFIX = "ncs_reference_"
WRITE_STATEMENT = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", re.IGNORECASE)
TEMP_TARGET = re.compile(r"\bTEMP(ORARY)?\b|\btemp_\w+", re.IGNORECASE)


class StatementAudit:
    """SQLite trace callback that classifies serving SQL without keeping it.

    Legacy tables are SQF and learning-module tables; framework tables hold the
    imported reference documents (the HRD guide among them). Temp-table writes
    stay separate from writes to the database file.
    """

    def __init__(self, conn: Any) -> None:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")]
        self.legacy_tables = sorted(name for name in names if any(m in name.lower() for m in LEGACY_TABLE_MARKERS))
        self.framework_tables = sorted(name for name in names if name.lower().startswith(FRAMEWORK_TABLE_PREFIX))
        self._legacy = self._pattern(self.legacy_tables)
        self._framework = self._pattern(self.framework_tables)
        self.counts: Counter[str] = Counter()

    @staticmethod
    def _pattern(names: list[str]) -> re.Pattern[str] | None:
        return re.compile(r"\b(?:" + "|".join(map(re.escape, names)) + r")\b", re.IGNORECASE) if names else None

    def __call__(self, sql: str) -> None:
        self.counts["statements"] += 1
        if self._legacy and self._legacy.search(sql):
            self.counts["legacy"] += 1
        if self._framework and self._framework.search(sql):
            self.counts["framework"] += 1
        if WRITE_STATEMENT.match(sql):
            self.counts["temp_writes" if TEMP_TARGET.search(sql) else "writes"] += 1


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def scenario_bootstrap(ratios: list[float], resamples: int = RESAMPLES) -> dict[str, float] | None:
    """Percentile CI of the scenario macro mean; slots inside a scenario stay clustered."""
    if not ratios:
        return None
    rng = random.Random(SEED)
    means = [sum(ratios[rng.randrange(len(ratios))] for _ in ratios) / len(ratios) for _ in range(resamples)]
    return {"lower": nearest_rank(means, 0.025), "upper": nearest_rank(means, 0.975), "level": 0.95,
            "method": "scenario_bootstrap", "resamples": resamples, "seed": SEED}


def ratio_status(value: float | None, metric: dict[str, Any]) -> str:
    """Map a higher-is-better ratio onto the contract's target/warning/action bands."""
    if value is None:
        return "unknown"
    if value >= metric["target"]["value"]:
        return "pass"
    if value < metric["action"]["below"]:
        return "fail"
    return "warn"


def git_identity() -> dict[str, Any]:
    def run(*args: str) -> bytes:
        return subprocess.run(["git", *GIT_FLAGS, *args], cwd=ROOT, capture_output=True, check=True,
                              creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0).stdout
    try:
        patch = run("diff", "HEAD", "--", ".", *LFS_EXCLUDES)
        return {"code_commit": run("rev-parse", "HEAD").decode().strip(),
                "working_tree_patch_sha256": sha_bytes(patch), "working_tree_dirty": bool(patch)}
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"code_commit": None, "working_tree_patch_sha256": None, "error": type(exc).__name__}


def measure_k10(conn: Any, metric: dict[str, Any]) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT c.major_code, COALESCE(s.collection_status, 'not_attempted') AS status, COUNT(*) AS units
        FROM competency_units AS cu
        JOIN classifications AS c ON c.classification_id = cu.classification_id
        LEFT JOIN ncs_qualification_collection_status AS s ON s.unit_code = cu.unit_code
        GROUP BY c.major_code, status
        ORDER BY c.major_code, status
        """
    ).fetchall()
    total: Counter[str] = Counter()
    majors: dict[str, Counter[str]] = {}
    for row in rows:
        total[row["status"]] += row["units"]
        majors.setdefault(row["major_code"], Counter())[row["status"]] += row["units"]
    denominator = sum(total.values())
    numerator = total["collected"] + total["empty"]
    value = numerator / denominator if denominator else None
    return {
        "metric_id": "K10", "numerator": numerator, "denominator": denominator, "value": value,
        "status": ratio_status(value, metric) if denominator else "unknown",
        "status_counts": dict(sorted(total.items())),
        "attempted_ratio": (denominator - total["not_attempted"]) / denominator if denominator else None,
        "per_major": {major: {"value": (c["collected"] + c["empty"]) / sum(c.values()), "units": sum(c.values()),
                              "status_counts": dict(sorted(c.items()))} for major, c in sorted(majors.items())},
        "limitations": ["empty means no qualification rows for that unit under the query; it is not positive evidence",
                        "error units are excluded from the numerator and kept in the denominator"],
    }


def slot_is_direct(slot: dict[str, Any], valid_ids: set[int], seen: set[int]) -> bool:
    course_id = slot.get("training_course_id")
    if not isinstance(course_id, int) or course_id not in valid_ids or course_id in seen:
        return False
    seen.add(course_id)
    # goal_direct_concept_hits counts training_goal_concept_text links whose
    # concept is in the resolved target scope or gap KSA set.
    return int(slot.get("goal_direct_concept_hits") or 0) > 0


def scenario_slots(result: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    from ncs_mcp.training_recommendation import _transition_case_course_evidence

    items = [item for item in (result.get("recommendations") or [])[:limit] if isinstance(item, dict)]
    evidence = _transition_case_course_evidence(items, limit=limit)
    slots = []
    for item, row in zip(items, evidence):
        match = item.get("match") or {}
        course = item.get("training_course") or {}
        slots.append({
            "rank": item.get("rank"),
            "training_course_id": course.get("training_course_id"),
            "tier": (item.get("recommendation_tier") or {}).get("tier"),
            "scope_alignment": (row.get("course_scope_fit") or {}).get("alignment"),
            "direct_unit_evidence": bool(match.get("direct_unit_evidence")),
            "goal_direct_concept_hits": int(match.get("goal_direct_concept_hits") or 0),
            "goal_token_concept_hits": int(match.get("goal_token_concept_hits") or 0),
            "goal_concept_hits": int(match.get("goal_concept_hits") or 0),
        })
    return slots


def g03_violations(slots: list[dict[str, Any]], valid_ids: set[int]) -> Counter[str]:
    """G03 checks on returned slots: unknown source IDs and primary (core) slots
    that lack direct target-unit evidence or sit outside the target scope."""
    found: Counter[str] = Counter()
    for slot in slots:
        if slot.get("training_course_id") not in valid_ids:
            found["invalid_source_id"] += 1
        if slot.get("tier") != "primary":
            continue
        if not slot.get("direct_unit_evidence"):
            found["primary_without_direct_unit_evidence"] += 1
        if slot.get("scope_alignment") not in {"direct", "near"}:
            found["primary_outside_target_scope"] += 1
    return found


def gold_scenarios(conn: Any) -> list[Any]:
    return conn.execute(
        "SELECT * FROM training_transition_gold_scenarios WHERE review_status != 'rejected' ORDER BY scenario_id"
    ).fetchall()


def exception_code(exc: Exception) -> str:
    # A save=False call that tries to write hits the read-only connection.
    if isinstance(exc, sqlite3.OperationalError) and "readonly" in str(exc).lower():
        return "exception:readonly_write"
    return f"exception:{type(exc).__name__}"


def legacy_use_declared(result: dict[str, Any]) -> bool:
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    return bool(audit.get("sqf_used") or audit.get("learning_modules_used"))


def measure_k03(conn: Any, metric: dict[str, Any], *, resamples: int = RESAMPLES,
                audit: StatementAudit | None = None) -> dict[str, Any]:
    from ncs_mcp.training_recommendation import recommend_training_transition

    scenarios = gold_scenarios(conn)
    valid_ids = {int(row[0]) for row in conn.execute("SELECT training_course_id FROM ncs_training_courses")}
    fixture = [{key: row[key] for key in ("scenario_id", "current_query", "target_query", "major_code",
                                          "review_status")} for row in scenarios]
    cases: list[dict[str, Any]] = []
    outcome: Counter[str] = Counter()
    slot_kinds: Counter[str] = Counter()
    g03: Counter[str] = Counter()
    serving: Counter[str] = Counter()
    checked_slots = 0
    if audit is not None:
        conn.set_trace_callback(audit)
    for row in scenarios:
        before = Counter(audit.counts) if audit is not None else Counter()
        try:
            result = recommend_training_transition(
                conn, current_query=row["current_query"], target_query=row["target_query"],
                major_code=row["major_code"], limit=K03_LIMIT, save=False)
            error = None if result.get("ok") else (result.get("error") or {}).get("code", "not_ok")
        except Exception as exc:  # counted as zero slots, never removed from the denominator
            result, error = {}, exception_code(exc)
        if audit is not None:
            delta = audit.counts - before
            serving["scenarios_touching_legacy_tables"] += bool(delta["legacy"])
            serving["scenarios_touching_framework_tables"] += bool(delta["framework"])
            serving["scenarios_with_writes"] += bool(delta["writes"])
        serving["scenarios_declaring_legacy_use"] += legacy_use_declared(result)
        serving["readonly_write_errors"] += error == "exception:readonly_write"
        slots = scenario_slots(result, K03_LIMIT)
        g03.update(g03_violations(slots, valid_ids))
        checked_slots += len(slots)
        seen: set[int] = set()
        direct = sum(slot_is_direct(slot, valid_ids, seen) for slot in slots[:K03_SLOTS])
        for slot in slots[:K03_SLOTS]:
            slot_kinds["goal_text_direct" if slot["goal_direct_concept_hits"] else
                       "goal_token_only" if slot["goal_token_concept_hits"] else
                       "unit_direct_without_goal_text" if slot["direct_unit_evidence"] else "other_supporting"] += 1
        slot_kinds["empty"] += K03_SLOTS - min(K03_SLOTS, len(slots))
        outcome["ok" if error is None else error] += 1
        cases.append({"scenario_id": row["scenario_id"], "major_code": row["major_code"], "error": error,
                      "direct_slots": direct, "ratio": direct / K03_SLOTS, "slots": slots})
    if audit is not None:
        conn.set_trace_callback(None)
    ratios = [case["ratio"] for case in cases]
    value = sum(ratios) / len(ratios) if ratios else None
    majors: dict[str, list[float]] = {}
    for case in cases:
        majors.setdefault(case["major_code"] or "unknown", []).append(case["ratio"])
    return {
        "metric_id": "K03", "numerator": sum(case["direct_slots"] for case in cases),
        "denominator": K03_SLOTS * len(cases), "sample_count": len(cases), "value": value,
        "confidence_interval": scenario_bootstrap(ratios, resamples),
        # The gold set has no preverified >=3 evidence-backed candidates per case
        # and no expected_action labels, so the contract's scope is not met.
        "status": "inconclusive" if cases else "unknown",
        "band_if_scope_were_met": ratio_status(value, metric) if cases else "unknown",
        "excluded_count": 0, "outcome_counts": dict(sorted(outcome.items())),
        "slot_kind_counts": dict(sorted(slot_kinds.items())),
        "per_major": {major: {"value": sum(v) / len(v), "scenarios": len(v)} for major, v in sorted(majors.items())},
        "fixture_sha256": sha_bytes(json.dumps(fixture, ensure_ascii=False, sort_keys=True).encode("utf-8")),
        "cases": cases,
        "limitations": [
            "structural evidence metric; not semantic correctness",
            "clarification and error responses stay in the denominator with zero direct slots",
            "gold scenarios lack preverified candidate availability and expected_action labels",
        ],
        "g03": {
            "guardrail_id": "G03", "violation_count": sum(g03.values()),
            "status": "pass" if not g03 else "fail", "violations_by_kind": dict(sorted(g03.items())),
            "checked_slots": checked_slots, "scope": f"gold transition fixture, top-{K03_LIMIT} recommendations",
            "limitations": ["covers returned slots of this fixture only, not every response or surface",
                            "cross_scope_name_only links are not identified separately here"],
        },
        "serving": dict(serving),
    }


def load_harness() -> Any:
    """The AI-HR plan validators live in the harness; load it like the accuracy audit does."""
    import importlib.util

    if "ncs_harness" in sys.modules:
        return sys.modules["ncs_harness"]
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))  # the harness imports sibling scripts
    spec = importlib.util.spec_from_file_location("ncs_harness", ROOT / "scripts" / "ncs_harness.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ncs_harness"] = module
    spec.loader.exec_module(module)
    return module


def plan_contract_gaps(harness: Any, plan: dict[str, Any]) -> dict[str, list[str]]:
    matrix = plan.get("training_system_matrix")
    gaps = {
        "matrix": harness._missing_aihr_matrix_fields(matrix if isinstance(matrix, list) else []),
        "plan": harness._missing_aihr_plan_fields(plan),
        "guide_trace": harness._missing_aihr_guide_trace_fields(plan),
        "query_route": harness._missing_aihr_query_route_fields(plan),
    }
    if not (isinstance(matrix, list) and matrix and all(isinstance(row, dict) for row in matrix)):
        gaps["matrix"].append("training_system_matrix.nonempty_list_of_rows")
    return gaps


@contextmanager
def read_only_db(db_path: Path, audit: StatementAudit | None) -> Iterator[Any]:
    from ncs_mcp.db import connect

    conn = connect(db_path, read_only=True)
    if audit is not None:
        conn.set_trace_callback(audit)
    try:
        yield conn
    finally:
        conn.close()


def run_plans(db_path: Path, scenarios: list[Any], audit: StatementAudit | None, serving: Counter[str],
              constraints: dict[str, Any] | None = None) -> Iterator[tuple[Any, dict[str, Any], str | None]]:
    """Call the internal planner tool for each scenario on a traced read-only DB."""
    from ncs_mcp import server

    original = server.open_recommendation_db
    server.open_recommendation_db = lambda: read_only_db(db_path, audit)
    try:
        for row in scenarios:
            before = Counter(audit.counts) if audit is not None else Counter()
            try:
                plan = server.plan_ncs_education_path(
                    current_query=row["current_query"], target_query=row["target_query"],
                    major_code=row["major_code"], limit=K03_LIMIT, **(constraints or {}))
                error = None if plan.get("ok") else (plan.get("error") or {}).get("code", "not_ok")
            except Exception as exc:
                plan, error = {}, exception_code(exc)
            if audit is not None:
                delta = audit.counts - before
                serving["plans_touching_legacy_tables"] += bool(delta["legacy"])
                serving["plans_touching_framework_tables"] += bool(delta["framework"])
                serving["plans_with_writes"] += bool(delta["writes"])
            serving["plans_declaring_legacy_use"] += legacy_use_declared(plan)
            serving["readonly_write_errors"] += error == "exception:readonly_write"
            yield row, plan, error
    finally:
        server.open_recommendation_db = original


def measure_g05_plans(db_path: Path, scenarios: list[Any], audit: StatementAudit | None = None,
                      harness: Any | None = None) -> dict[str, Any]:
    """Run the internal planner tool on the fixture and count missing contract fields.

    Only ok plans are checked for fields. A missing route contract is a gap even
    when the planner refuses; clarification and other refusals are counted, not
    treated as gaps or as plan successes.
    """
    harness = harness or load_harness()
    outcome: Counter[str] = Counter()
    gap_fields: Counter[str] = Counter()
    serving: Counter[str] = Counter()
    plans_with_gaps = []
    for row, plan, error in run_plans(db_path, scenarios, audit, serving):
        outcome["ok" if error is None else error] += 1
        if error == "missing_query_route_contract":
            gaps = {"query_route": list(plan.get("missing_query_route_fields") or ["query_route"])}
        elif error is None:
            gaps = plan_contract_gaps(harness, plan)
        else:
            continue
        if any(gaps.values()):
            plans_with_gaps.append({"scenario_id": row["scenario_id"], "gaps": gaps})
            for kind, fields in gaps.items():
                gap_fields.update(f"{kind}:{field}" for field in fields)
    violation_count = sum(gap_fields.values())
    checked = outcome["ok"] + outcome["missing_query_route_contract"]
    return {
        "guardrail_id": "G05", "scope": "internal plan_ncs_education_path on the gold transition fixture",
        "violation_count": violation_count if checked else None,
        "status": ("pass" if not violation_count else "fail") if checked else "unknown",
        "plans_checked": checked, "outcome_counts": dict(sorted(outcome.items())),
        "plans_with_gaps": plans_with_gaps, "gap_field_counts": dict(sorted(gap_fields.items())),
        "serving": dict(serving),
        "limitations": ["release and dashboard static proof artifacts are not audited here",
                        "the public facade (planner disabled on the public surface) is not called",
                        "refusals other than a missing route contract are counted, not inspected"],
    }


def _number(value: Any) -> float | None:
    try:
        return None if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None


def g06_row_violations(row: dict[str, Any]) -> list[str]:
    """Fit claims a matrix row's own evidence does not support, and unfit rows shown as ready.

    unknown, partial, mismatch and over are not violations by themselves; they
    become one when the row still reads as ready or the summary still says fit.
    """
    delivery = row.get("delivery_operation") if isinstance(row.get("delivery_operation"), dict) else {}
    course = row.get("course_fit") if isinstance(row.get("course_fit"), dict) else {}
    method = delivery.get("method_constraint_fit") or {}
    time = delivery.get("time_constraint_fit") or {}
    facility = row.get("facility_constraint_fit") or delivery.get("facility_constraint_fit") or {}
    found = []
    if method.get("status") == "fit" and (method.get("missing_method_groups") or not method.get("matched_method_groups")
                                          or not course.get("methods")):
        found.append("method_fit_unsupported")
    actual, limit = _number(time.get("actual_hours")), _number(time.get("preferred_max_hours"))
    if time.get("status") == "fit" and (actual is None or limit is None or actual > limit
                                        or _number(course.get("hours")) != actual):
        found.append("time_fit_unsupported")
    if facility.get("status") == "fit" and (facility.get("missing") or not facility.get("matched")
                                            or not course.get("facilities")):
        found.append("facility_fit_unsupported")
    requested = {name: str(fit.get("status") or "unknown") for name, fit in
                 (("method", method), ("time", time), ("facility", facility))
                 if fit.get("status") not in (None, "", "not_requested")}
    unfit = {name: status for name, status in requested.items() if status != "fit"}
    if (delivery.get("constraint_fit") or {}).get("status") == "fit" and unfit:
        found.append("summary_fit_with_unfit_dimension")
    review = row.get("human_review") if isinstance(row.get("human_review"), dict) else {}
    flags = set(review.get("flags") or [])
    if unfit and review.get("severity") == "ready":
        found.append("unfit_dimension_marked_ready")
    found.extend(f"missing_review_flag:{name}_{status}" for name, status in sorted(unfit.items())
                 if f"delivery:{name}_{status}" not in flags)
    return found


G06_CONSTRAINTS = {"preferred_max_hours": 24, "preferred_methods": ["집체훈련"], "preferred_facilities": ["강의실"]}


def measure_g06_plans(db_path: Path, scenarios: list[Any], audit: StatementAudit | None = None,
                      constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    constraints = constraints or G06_CONSTRAINTS
    outcome: Counter[str] = Counter()
    violations: Counter[str] = Counter()
    summary_status: Counter[str] = Counter()
    serving: Counter[str] = Counter()
    rows_checked = 0
    for _, plan, error in run_plans(db_path, scenarios, audit, serving, constraints):
        outcome["ok" if error is None else error] += 1
        matrix = plan.get("training_system_matrix") if error is None else None
        for row in matrix if isinstance(matrix, list) else []:
            if not isinstance(row, dict):
                continue
            rows_checked += 1
            violations.update(g06_row_violations(row))
            summary_status[str(((row.get("delivery_operation") or {}).get("constraint_fit") or {}).get("status"))] += 1
    violation_count = sum(violations.values())
    return {
        "guardrail_id": "G06", "scope": "internal plan_ncs_education_path on the gold transition fixture "
                                        "with one fixed delivery constraint profile",
        "constraints": constraints,
        "violation_count": violation_count if rows_checked else None,
        "status": ("pass" if not violation_count else "fail") if rows_checked else "unknown",
        "rows_checked": rows_checked, "outcome_counts": dict(sorted(outcome.items())),
        "constraint_fit_status_counts": dict(sorted(summary_status.items())),
        "violations_by_kind": dict(sorted(violations.items())),
        "serving": dict(serving),
        "limitations": ["one constraint profile; level is not a requested constraint in this fixture",
                        "unknown-evidence rows are checked only when the fixture produces them "
                        "(see constraint_fit_status_counts)",
                        "annual operation plan feasibility wording is not inspected"],
    }


def serving_guardrails(audit: StatementAudit | None, serving: Counter[str],
                       planner_ran: bool = True) -> dict[str, dict[str, Any]]:
    """G04 and G08 from the traced save=False recommender and planner calls."""
    tools = "recommend_training_transition and plan_ncs_education_path" if planner_ran else "recommend_training_transition"
    scope = f"save=False {tools} on the gold transition fixture"
    if audit is None:
        reason = "SQL trace not collected."
        return {key: {"guardrail_id": key, "violation_count": None, "status": "unknown", "reason": reason}
                for key in ("G04", "G08")}
    legacy = (serving["scenarios_touching_legacy_tables"] + serving["plans_touching_legacy_tables"]
              + serving["scenarios_declaring_legacy_use"] + serving["plans_declaring_legacy_use"])
    framework = serving["scenarios_touching_framework_tables"] + serving["plans_touching_framework_tables"]
    writes = serving["scenarios_with_writes"] + serving["plans_with_writes"] + serving["readonly_write_errors"]
    g04 = {
        "guardrail_id": "G04", "scope": scope, "violation_count": legacy,
        # Framework tables may back the guide trace; access alone cannot show
        # guide examples being used as source facts or scores, so it is held for review.
        "status": "fail" if legacy else "review_required" if framework else "pass",
        "legacy_tables": len(audit.legacy_tables), "framework_tables": len(audit.framework_tables),
        "statements_checked": audit.counts["statements"],
        "legacy_table_statements": audit.counts["legacy"], "framework_table_statements": audit.counts["framework"],
        "runs_touching_framework_tables": framework,
        "counts": {key: value for key, value in sorted(serving.items()) if "legacy" in key or "framework" in key},
        "limitations": ["Builder link generation and stored scores are not audited",
                        "guide example text copied into non-framework tables would not be detected"],
    }
    g08 = {
        "guardrail_id": "G08", "scope": scope, "violation_count": writes,
        "status": "fail" if writes else "pass",
        "write_statements": audit.counts["writes"], "temp_table_writes": audit.counts["temp_writes"],
        "readonly_write_errors": serving["readonly_write_errors"],
        "limitations": ["qualification collection cooldown, retry, batch and preflight limits are not audited here",
                        "overlapping broad writers outside this process are not detected"],
    }
    return {"G04": g04, "G08": g08}


TRUSTED_REVIEW_STATUSES = ("human_reviewed", "accepted", "reviewed")
# review_audit_log entity_type and id column for tables that carry review_status.
# Learning-module tables are legacy and not read by the active recommender (G04).
G02_AUDIT_ENTITIES = {
    "training_transition_gold_scenarios": ("training_transition_gold_scenario", "scenario_id"),
    "ncs_career_paths": ("ncs_career_path", "career_path_id"),
    "ncs_query_aliases": ("ncs_query_alias", "alias_id"),
    "ontology_concept_label_candidates": ("ontology_concept_label_candidate", "label_id"),
    "ontology_concepts": ("ontology_concept", "concept_id"),
}
# Same as ncs_dashboard.SYNTHETIC_REVIEW_PACKET_PREFIXES: dashboard decision ids, not packet files.
SYNTHETIC_PACKET_PREFIXES = ("ksa_review_dashboard_one_click_v1:", "ksa_definition_dashboard_review_v1:")


def packet_blockers(audit: Any, status: str) -> list[str]:
    """Packet-backed provenance checks of the quality gate, without its scenario-only action name."""
    from ncs_mcp.quality_gates import (
        AUTOMATED_REVIEWER_IDS, _evidence_refs_json_is_packet_backing_list, _resolve_review_packet_artifact)
    from ncs_mcp.review_safety import review_packet_sha256

    blockers = []
    if str(audit["new_status"] or "").strip() != status:
        blockers.append("new_status_matches_row")
    reviewer = str(audit["reviewer_id"] or "").strip()
    if not reviewer or reviewer.lower() in AUTOMATED_REVIEWER_IDS:
        blockers.append("human_reviewer_id")
    packet = str(audit["source_decision_packet"] or "").strip()
    packet_path = _resolve_review_packet_artifact(packet) if packet else None
    if packet_path is None:
        blockers.append("source_decision_packet_resolves_to_reports_artifact")
    digest = str(audit["source_artifact_hash"] or "").strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or (
            packet_path is not None and digest != "sha256:" + review_packet_sha256(packet_path)):
        blockers.append("source_artifact_hash_matches_reports_artifact")
    if not str(audit["rationale"] or "").strip():
        blockers.append("rationale")
    if not _evidence_refs_json_is_packet_backing_list(audit["evidence_refs_json"]):
        blockers.append("evidence_refs_json_valid")
    return blockers


def measure_g02(conn: Any) -> dict[str, Any]:
    """Trusted review statuses without a packet-backed audit row, and promoted boilerplate definitions."""
    from ncs_mcp.db import _is_ksa_definition_boilerplate

    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    placeholders = ",".join("?" for _ in TRUSTED_REVIEW_STATUSES)
    trusted_by_table: dict[str, int] = {}
    unbacked: Counter[str] = Counter()
    legacy_unbacked: Counter[str] = Counter()
    blocker_counts: Counter[str] = Counter()
    unbacked_reason: Counter[str] = Counter()
    packet_backed = 0
    for table in tables:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "review_status" not in columns:
            continue
        count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE review_status IN ({placeholders})",
                             TRUSTED_REVIEW_STATUSES).fetchone()[0]
        if not count:
            continue
        trusted_by_table[table] = count
        if any(marker in table for marker in LEGACY_TABLE_MARKERS):
            legacy_unbacked[table] += count
            continue
        if table not in G02_AUDIT_ENTITIES:
            unbacked[table] += count
            blocker_counts["no_audit_entity_mapping"] += count
            continue
        entity_type, id_column = G02_AUDIT_ENTITIES[table]
        audits: dict[str, list[Any]] = {}
        for audit in conn.execute("SELECT * FROM review_audit_log WHERE entity_type = ? ORDER BY id", (entity_type,)):
            audits.setdefault(str(audit["entity_id"]), []).append(audit)
        for row in conn.execute(f"SELECT {id_column} AS row_id, review_status FROM {table} "
                                f"WHERE review_status IN ({placeholders})", TRUSTED_REVIEW_STATUSES):
            rows = audits.get(str(row["row_id"]), [])
            candidates = [packet_blockers(audit, row["review_status"]) for audit in rows]
            if any(not blockers for blockers in candidates):
                packet_backed += 1
                continue
            unbacked[table] += 1
            best = min(candidates, key=len) if candidates else ["review_audit_log"]
            blocker_counts.update(best)
            synthetic = best == ["source_decision_packet_resolves_to_reports_artifact"] and any(
                str(audit["source_decision_packet"] or "").strip().lower().startswith(SYNTHETIC_PACKET_PREFIXES)
                for audit in rows)
            unbacked_reason["dashboard_decision_without_packet_file" if synthetic else "missing_packet_fields"] += 1
    promoted = 0
    for concept in conn.execute("SELECT concept_type, concept_name, definition FROM ontology_concepts "
                                f"WHERE definition_status = 'defined' OR review_status IN ({placeholders})",
                                TRUSTED_REVIEW_STATUSES):
        promoted += bool(_is_ksa_definition_boilerplate(concept["concept_type"] or "", concept["concept_name"] or "",
                                                        concept["definition"] or ""))
    violation_count = sum(unbacked.values()) + promoted
    return {
        "guardrail_id": "G02", "scope": "trusted review statuses in every table with review_status; "
                                        "defined or trusted ontology concept definitions",
        "violation_count": violation_count, "status": "pass" if not violation_count else "fail",
        "trusted_rows_by_table": trusted_by_table, "packet_backed_rows": packet_backed,
        "unbacked_trusted_rows_by_table": dict(sorted(unbacked.items())),
        "unbacked_blocker_counts": dict(sorted(blocker_counts.items())),
        # Human reviewer, hash, rationale and evidence present; only the packet is
        # a dashboard decision id. Whether that counts as a decision packet is a
        # human policy call, so it stays counted under the contract wording.
        "unbacked_by_reason": dict(sorted(unbacked_reason.items())),
        "legacy_trusted_rows_not_counted": dict(sorted(legacy_unbacked.items())),
        "promoted_boilerplate_definitions": promoted,
        "on_violation": "exclude the rows from trusted KPI evidence and queue re-confirmation; no automatic status change",
        "limitations": ["audit action names differ by table and are not checked",
                        "legacy learning-module tables are listed but not counted; the active recommender does not read them",
                        "claims in reports or responses (for example llm_reviewed called verified) are not scanned"],
    }


def env_secret_values(env_path: Path) -> list[str]:
    values = []
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        name, sep, value = line.strip().partition("=")
        value = value.strip().strip("\"'")
        if sep and not name.startswith("#") and len(value) >= 8 and any(
                marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            values.append(value)
    return values


def files_containing(values: list[str], paths: list[Path], max_bytes: int = 5_000_000) -> list[str]:
    """Return paths whose bytes contain any value; never returns the values."""
    needles = [value.encode("utf-8") for value in values]
    hits = []
    for path in paths:
        try:
            if path.stat().st_size > max_bytes:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if any(needle in data for needle in needles):
            hits.append(str(path))
    return hits


def measure_g10_tracked_secrets() -> dict[str, Any]:
    values = env_secret_values(ROOT / ".env")
    report: dict[str, Any] = {"guardrail_id": "G10", "scope": "secret values from .env in git-tracked files under 5 MB",
                              "secret_values_checked": len(values)}
    if not values:
        report.update({"violation_count": None, "status": "unknown", "reason": "No .env secret values to compare."})
        return report
    try:
        listed = subprocess.run(["git", *GIT_FLAGS, "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        report.update({"violation_count": None, "status": "unknown", "reason": f"git ls-files failed: {type(exc).__name__}"})
        return report
    paths = [ROOT / name for name in listed.decode("utf-8", "replace").split("\0") if name]
    hits = files_containing(values, paths)
    report.update({"files_checked": len(paths), "violation_count": len(hits), "files": hits,
                   "status": "pass" if not hits else "fail",
                   "limitations": ["untracked reports, published artifacts and remote outputs are not scanned",
                                   "public-claim wording (approval, certification, legal eligibility) is not checked"]})
    return report


def ksa_manifest(conn: Any) -> dict[int, str]:
    manifest = {}
    for ksa_id, text in conn.execute("SELECT ksa_id, ksa_text_raw FROM ksa_items ORDER BY ksa_id"):
        manifest[int(ksa_id)] = sha_bytes(("" if text is None else str(text)).encode("utf-8"))
    return manifest


def write_manifest(path: Path, manifest: dict[int, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as stream:
        for ksa_id, digest in manifest.items():
            stream.write(f"{ksa_id}\t{digest}\n")


def read_manifest(path: Path) -> dict[int, str]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return {int(key): value for key, value in (line.rstrip("\n").split("\t") for line in stream if line.strip())}


def measure_g01(conn: Any, previous: Path | None, current_out: Path | None) -> dict[str, Any]:
    manifest = ksa_manifest(conn)
    digest = sha_bytes("".join(f"{k}\t{v}\n" for k, v in manifest.items()).encode("utf-8"))
    if current_out:
        write_manifest(current_out, manifest)
    report: dict[str, Any] = {"guardrail_id": "G01", "row_count": len(manifest), "manifest_sha256": digest,
                              "manifest_path": str(current_out) if current_out else None}
    if previous is None or not previous.exists():
        report.update({"violation_count": None, "status": "unknown",
                       "reason": "No earlier full row-hash manifest to compare against."})
        return report
    before = read_manifest(previous)
    changed = sum(1 for key, value in before.items() if key in manifest and manifest[key] != value)
    deleted = sum(1 for key in before if key not in manifest)
    added = sum(1 for key in manifest if key not in before)
    report.update({"compared_with": str(previous), "changed": changed, "deleted": deleted, "added": added,
                   "violation_count": changed + deleted, "status": "pass" if changed + deleted == 0 else "fail",
                   "limitations": ["added rows are reported separately; legitimacy of a new source version is not judged here"]})
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "processed" / "ncs.db")
    parser.add_argument("--definition", type=Path, default=ROOT / "docs" / "ncs_kpi_guardrails.v1.json")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ksa-manifest-out", type=Path)
    parser.add_argument("--previous-ksa-manifest", type=Path)
    parser.add_argument("--skip-k03", action="store_true", help="Skip the gold-scenario recommender run")
    parser.add_argument("--skip-plans", action="store_true", help="Skip the gold-scenario planner run (G05)")
    parser.add_argument("--skip-db-hash", action="store_true", help="Record size and mtime only")
    args = parser.parse_args(argv)
    if args.out.resolve() == args.db.resolve():
        parser.error("--out must not be the database")
    from ncs_mcp.db import connect

    definition = json.loads(args.definition.read_text(encoding="utf-8"))
    metrics = {item["id"]: item for item in definition["metrics"]}
    started = datetime.now(UTC).isoformat()
    db_stat = args.db.stat()
    report: dict[str, Any] = {
        "schema": SCHEMA, "definition_version": definition.get("version"), "observed_at": started,
        "report_only": True, "db_writes": False, "network_calls": False, "status_update_allowed": False,
        "approval_claim": False, "surface": "local_full_db_function", **git_identity(),
        "database": {"path": str(args.db), "bytes": db_stat.st_size,
                     "mtime": datetime.fromtimestamp(db_stat.st_mtime, UTC).isoformat(),
                     "sha256": None if args.skip_db_hash else sha_file(args.db)},
    }
    conn = connect(args.db, read_only=True)
    try:
        measured = {"K10": measure_k10(conn, metrics["K10"])}
        guardrails = {"G01": measure_g01(conn, args.previous_ksa_manifest, args.ksa_manifest_out),
                      "G02": measure_g02(conn), "G10": measure_g10_tracked_secrets()}
        if not args.skip_k03:
            audit = StatementAudit(conn)
            measured["K03"] = measure_k03(conn, metrics["K03"], audit=audit)
            guardrails["G03"] = measured["K03"].pop("g03")
            serving = Counter(measured["K03"].pop("serving"))
            if not args.skip_plans:
                guardrails["G05"] = measure_g05_plans(args.db, gold_scenarios(conn), audit)
                guardrails["G06"] = measure_g06_plans(args.db, gold_scenarios(conn), audit)
                for key in ("G05", "G06"):
                    serving.update(guardrails[key].pop("serving"))
            guardrails.update(serving_guardrails(audit, serving, planner_ran=not args.skip_plans))
    finally:
        conn.close()
    report["metrics"] = [measured.get(key) or {"metric_id": key, "value": None, "status": "unknown",
                                               "reason": "not measured by this local snapshot"}
                         for key in sorted(metrics)]
    report["guardrails"] = [guardrails.get(item["id"]) or {"guardrail_id": item["id"], "violation_count": None,
                                                           "status": "unknown", "reason": "not audited by this snapshot"}
                            for item in definition["guardrails"]]
    report["window_end"] = datetime.now(UTC).isoformat()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    summary = {item["metric_id"]: [item.get("value"), item.get("status")] for item in report["metrics"]}
    summary.update({item["guardrail_id"]: [item.get("violation_count"), item.get("status")]
                    for item in report["guardrails"] if item["status"] != "unknown"})
    print(json.dumps({"out": str(args.out), "summary": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
