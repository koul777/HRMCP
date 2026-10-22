"""Measure the locally measurable parts of the NCS KPI v1 contract.

Report-only: opens the DB read-only, calls recommenders with save=False, makes
no network calls and writes nothing but the requested output files. It covers
K10 (census), a K03 structural diagnostic on the gold transition scenarios, and
the G01 source-KSA manifest comparison. Everything else stays unknown here.
A pass here is not human approval, release readiness or deployment evidence.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import random
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    slots = []
    for item in (result.get("recommendations") or [])[:limit]:
        match = item.get("match") or {}
        course = item.get("training_course") or {}
        slots.append({
            "rank": item.get("rank"),
            "training_course_id": course.get("training_course_id"),
            "tier": (item.get("recommendation_tier") or {}).get("tier"),
            "direct_unit_evidence": bool(match.get("direct_unit_evidence")),
            "goal_direct_concept_hits": int(match.get("goal_direct_concept_hits") or 0),
            "goal_token_concept_hits": int(match.get("goal_token_concept_hits") or 0),
            "goal_concept_hits": int(match.get("goal_concept_hits") or 0),
        })
    return slots


def measure_k03(conn: Any, metric: dict[str, Any], *, resamples: int = RESAMPLES) -> dict[str, Any]:
    from ncs_mcp.training_recommendation import recommend_training_transition

    scenarios = conn.execute(
        "SELECT * FROM training_transition_gold_scenarios WHERE review_status != 'rejected' ORDER BY scenario_id"
    ).fetchall()
    valid_ids = {int(row[0]) for row in conn.execute("SELECT training_course_id FROM ncs_training_courses")}
    fixture = [{key: row[key] for key in ("scenario_id", "current_query", "target_query", "major_code",
                                          "review_status")} for row in scenarios]
    cases: list[dict[str, Any]] = []
    outcome: Counter[str] = Counter()
    slot_kinds: Counter[str] = Counter()
    for row in scenarios:
        try:
            result = recommend_training_transition(
                conn, current_query=row["current_query"], target_query=row["target_query"],
                major_code=row["major_code"], limit=K03_LIMIT, save=False)
            error = None if result.get("ok") else (result.get("error") or {}).get("code", "not_ok")
        except Exception as exc:  # counted as zero slots, never removed from the denominator
            result, error = {}, f"exception:{type(exc).__name__}"
        slots = scenario_slots(result, K03_LIMIT)
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
    }


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
        if not args.skip_k03:
            measured["K03"] = measure_k03(conn, metrics["K03"])
        guardrails = {"G01": measure_g01(conn, args.previous_ksa_manifest, args.ksa_manifest_out)}
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
