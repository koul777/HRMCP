"""Same-DB read-only accuracy evidence; development checks are not semantic gold.

Run each source tree in a fresh windowless Python process. Existing repository
auditors supply retrieval metrics and planner validators; this wrapper supplies
identity, source isolation, comparison, proof checks and interpretation limits.
No hidden holdout or recorded metric baseline is read.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = {
    "dev90": "tests/fixtures/ncs_search_eval_nl_dev.json",
    "regression40": "tests/fixtures/ncs_search_eval_nl.json",
    "dev_long": "tests/fixtures/ncs_search_eval_nl_dev_long.json",
}
METRICS = ("hit_at_1", "hit_at_3", "mrr")
# Package-local configuration, vocabulary, schema and presentation resources.
# Do not fingerprint external workspace files, credentials or databases.
RUNTIME_RESOURCE_SUFFIXES = frozenset({
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sql",
    ".txt", ".md", ".csv", ".tsv", ".xml", ".html", ".css", ".js",
    ".j2", ".jinja", ".jinja2",
})
TRANSIENT_SOURCE_DIRS = frozenset({"__pycache__", "cache", "caches", "node_modules"})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_identity(path: Path) -> dict:
    path = path.resolve(strict=True)
    stat = path.stat()
    return {"path": str(path), "bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "sha256": sha256(path)}


def tree_identity(source: Path) -> dict:
    source = source.resolve(strict=True)
    files = {}
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part.startswith(".") or part in TRANSIENT_SOURCE_DIRS for part in relative.parts):
            continue
        eligible = path.suffix == ".py" or (
            relative.parts[0] == "ncs_mcp" and path.suffix.lower() in RUNTIME_RESOURCE_SUFFIXES)
        if not eligible or not path.is_file():
            continue
        if not path.resolve(strict=True).is_relative_to(source):
            raise ValueError("source resource must stay inside source-root")
        files[relative.as_posix()] = sha256(path)
    if "ncs_mcp/server.py" not in files:
        raise ValueError("source-root must contain ncs_mcp/server.py")
    return {"path": str(source), "files": files,
            "fingerprint_policy": {"version": "python_and_package_resources_v1",
                                   "resource_root": "ncs_mcp",
                                   "resource_suffixes": sorted(RUNTIME_RESOURCE_SUFFIXES),
                                   "excluded_directories": sorted(TRANSIENT_SOURCE_DIRS),
                                   "exclude_hidden_paths": True},
            "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


def database_identity(db: Path) -> dict:
    result = file_identity(db)
    # A WAL can change the read-only logical DB without changing the main file.
    result["sidecars"] = {suffix: file_identity(Path(str(db) + suffix))
                          for suffix in ("-wal", "-journal") if Path(str(db) + suffix).is_file()}
    return result


def validate_outputs(db: Path, source_roots: list[Path], output_dir: Path) -> Path:
    """Reject source/DB aliases before creating any report or subprocess."""
    output_dir = output_dir.resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError("output-dir must be a directory")
    for source in source_roots:
        source = source.resolve(strict=True)
        if output_dir == source or output_dir.is_relative_to(source):
            raise ValueError("output-dir must be outside source roots")
    if db.resolve().is_relative_to(output_dir):
        raise ValueError("output-dir must not contain the input database")
    # Protect existing hardlinks as well as symbolic links to inputs.
    for output in (output_dir / n for n in ("baseline.json", "candidate.json", "bundle.json", "bundle.md")):
        if output.exists() and (output.resolve() == db.resolve() or output.samefile(db)):
            raise ValueError("output must not overwrite the database")
        if any(output.resolve().is_relative_to(s.resolve()) for s in source_roots):
            raise ValueError("output must not overwrite runtime source")
    return output_dir


def artifact_proofs(paths: list[Path]) -> list[dict]:
    rows = []
    for path in paths:
        exists = path.is_file()
        non_empty = exists and path.stat().st_size > 0
        row = {"path": str(path.resolve()), "exists": exists, "non_empty": non_empty}
        if non_empty:
            row.update(file_identity(path))
        rows.append(row)
    return rows


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def runtime_isolation(source: Path) -> dict:
    modules = {name: str(Path(module.__file__).resolve())
               for name, module in sys.modules.items()
               if (name == "ncs_mcp" or name.startswith("ncs_mcp.")) and getattr(module, "__file__", None)}
    leaked = {name: path for name, path in modules.items()
              if not Path(path).is_relative_to(source.resolve())}
    return {"ok": bool(modules) and not leaked, "loaded_module_count": len(modules),
            "loaded_server": modules.get("ncs_mcp.server"), "leaked_modules": leaked}


def reviewed_transition_sample(conn, training, quality, limit: int) -> dict:
    status_counts = {str(row[0]): row[1] for row in conn.execute(
        "SELECT review_status, count(*) FROM training_transition_gold_scenarios GROUP BY review_status")}
    provenance = quality._transition_packet_backed_trusted_scenario_provenance(conn, status_counts)
    statuses = list(training.TRUSTED_TRANSITION_REVIEW_STATUSES)
    placeholders = ",".join("?" for _ in statuses)
    ids = [row[0] for row in conn.execute(
        f"SELECT scenario_id FROM training_transition_gold_scenarios WHERE review_status IN ({placeholders}) "
        "ORDER BY scenario_id LIMIT ?", (*statuses, limit))] if limit else []
    result = training.evaluate_training_transition_scenarios(
        conn, limit=5, scenario_ids=ids, review_statuses=statuses,
        scenario_limit=len(ids)) if ids else None
    # Existing evaluator's closed-list ranking statistics are diagnostic only;
    # review statuses alone cannot establish human provenance or exhaustive labels.
    return {"status": "review_required" if ids else "not_evaluated",
            "scenario_ids": ids, "status_counts": status_counts, "provenance": provenance,
            "existing_review_rows": conn.execute("SELECT count(*) FROM training_transition_scenario_reviews").fetchone()[0],
            "expected_course_labels_complete": False, "human_relevance_claim": False,
            "precision_claim_allowed": False,
            "label_interpretation": "stored review labels; human provenance assessed separately; course labels incomplete",
            "diagnostic_evaluation": result}


def output_contract(harness, payload: dict) -> dict:
    matrix = payload.get("training_system_matrix")
    matrix_valid = isinstance(matrix, list) and bool(matrix) and all(isinstance(row, dict) for row in matrix)
    missing = {
        "missing_matrix_fields": harness._missing_aihr_matrix_fields(matrix if isinstance(matrix, list) else []),
        "missing_plan_fields": harness._missing_aihr_plan_fields(payload),
        "missing_guide_trace_fields": harness._missing_aihr_guide_trace_fields(payload),
        "missing_query_route_fields": harness._missing_aihr_query_route_fields(payload),
    }
    if not matrix_valid:
        missing["missing_matrix_fields"].append("training_system_matrix.nonempty_list_of_rows")
    return {"status": "checked", "ok": payload.get("ok") is True and matrix_valid and not any(missing.values()),
            "matrix_row_count": len(matrix) if isinstance(matrix, list) else 0,
            "empty_matrix": not matrix, **missing, "response": payload,
            "interpretation": "single development plan shape; not release readiness or relevance"}


def stage_failures(report: dict) -> list[str]:
    """Positive evidence is required even if a producer reports complete=true."""
    failures = []
    if report.get("source_unchanged") is False:
        failures.append("source:changed_during_stage")
    majors = set(report.get("available_major_codes") or [])
    for kind in ("name", "code"):
        section = report.get("exact", {}).get(kind, {})
        cases = section.get("cases")
        count = section.get("overall", {}).get("cases")
        if not isinstance(cases, list) or not cases or count != len(cases):
            failures.append(f"exact_{kind}:missing_or_empty_cases")
        if not majors or not majors.issubset(section.get("by_major", {})):
            failures.append(f"exact_{kind}:missing_major_coverage")
        if isinstance(cases, list) and any(row.get("error_code") for row in cases):
            failures.append(f"exact_{kind}:execution_error")
    for label in FIXTURES:
        current = report.get("development", {}).get(label, {}).get("evaluation", {}).get("current", {})
        cases = current.get("cases")
        overall = current.get("overall", {})
        if not isinstance(cases, list) or not cases or overall.get("case_count") != len(cases):
            failures.append(f"{label}:missing_or_empty_cases")
        if overall.get("search_error_count", 0) or (isinstance(cases, list) and any(row.get("error") for row in cases)):
            failures.append(f"{label}:search_execution_error")
    sample = report.get("reviewed_transition_sample")
    if not isinstance(sample, dict):
        failures.append("transition:missing_sample")
    elif sample.get("scenario_ids"):
        cases = (sample.get("diagnostic_evaluation") or {}).get("cases")
        if not isinstance(cases, list) or len(cases) != len(sample["scenario_ids"]) or any(row.get("ok") is not True for row in cases):
            failures.append("transition:missing_or_failed_scenario_evidence")
    elif any(sample.get("status_counts", {}).get(status, 0) for status in ("human_reviewed", "reviewed", "accepted")):
        failures.append("transition:stored_review_sample_not_evaluated")
    return failures


def worker(args) -> dict:
    db = args.db.resolve(strict=True)
    source = args.source_root.resolve(strict=True)
    before_db = database_identity(db)
    before_source = tree_identity(source)
    # Scripts may add ROOT/src at import time: finish importing the metric-only
    # auditors before choosing the package tree, then load server from that tree.
    exact = load_script("audit_ncs_exact_lookup")
    nl = load_script("audit_ncs_search_precision")
    sys.path.insert(0, str(source))
    os.environ.update(NCS_DB_PATH=str(db), NCS_MCP_READ_ONLY="1",
                      NCS_MCP_ENABLE_OPERATOR_TOOLS="0", NCS_MCP_OPERATOR_TOOLS="false")
    from ncs_mcp import server, training_recommendation, quality_gates
    # Validators are centralized in the harness. The already-imported package's
    # __path__ pins later ncs_mcp imports; verify every module's origin below.
    sys.path.insert(0, str(ROOT / "scripts"))
    harness = load_script("ncs_harness")
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    report = {"schema": "ncs_accuracy_source_evidence_v1", "generated_at": datetime.now(UTC).isoformat(),
              "execution_nonce": args.execution_nonce,
              "source_before": before_source, "database_before": before_db,
              "auditor_identity": {name: file_identity(ROOT / "scripts" / f"{name}.py") for name in
                                   ("audit_ncs_exact_lookup", "audit_ncs_search_precision", "ncs_harness")},
              "read_only": True, "db_writes": False, "human_approval_claim": False,
              "status_updates": False, "exact": {}, "development": {}, "errors": []}
    stage = "exact_lookup"
    try:
        rows = conn.execute("SELECT cu.unit_code, cu.unit_name_raw, c.major_code FROM competency_units cu "
                            "JOIN classifications c USING(classification_id) ORDER BY cu.unit_code").fetchall()
        report["available_major_codes"] = sorted({str(row[2]) for row in rows})
        report["unit_count"] = len(rows)
        if not rows:
            raise ValueError("database has no source units")
        for kind in ("name", "code"):
            cases = exact.build_cases(rows, kind=kind, per_major_limit=args.per_major_limit)
            records = exact.evaluate(cases, server.search_ncs, limit=3)
            report["exact"][kind] = {"evidence_kind": "source_self_retrieval_not_semantic_gold",
                                     "parameters": {"per_major_limit": args.per_major_limit, "limit": 3},
                                     **exact.aggregate(records), "cases": records}
        for label, relative in FIXTURES.items():
            stage = label
            fixture = args.fixture_root / relative
            fixture_before = file_identity(fixture)
            evaluation = nl.build_nl_evaluation_report(
                input_path=fixture, db_path=db, limit=10, hit3_threshold=0.7,
                enforce_hit3=False, compare_stage1_baseline=False,
                search_fn=lambda query, scope, limit: server.search_ncs(query, scope=scope, limit=limit))
            report["development"][label] = {"fixture": fixture_before,
                                           "fixture_unchanged": file_identity(fixture) == fixture_before,
                                           "interpretation": "code_reviewed_development_or_regression_not_human_relevance",
                                           "evaluation": evaluation}
            if evaluation["current"]["overall"].get("search_error_count"):
                report["errors"].append({"stage": stage, "type": "search_execution_error"})
        stage = "reviewed_transition_sample"
        report["reviewed_transition_sample"] = reviewed_transition_sample(
            conn, training_recommendation, quality_gates, args.transition_limit)
        if args.skip_plan:
            report["output_contract"] = {"status": "not_evaluated", "ok": False, "reason": "--skip-plan"}
        else:
            stage = "output_contract"
            payload = server.plan_ncs_education_path(current_query="노무관리", target_query="인사기획", limit=3, save=False)
            report["output_contract"] = output_contract(harness, payload)
    except Exception as exc:
        # Keep failures reviewable without exposing service keys or arbitrary text.
        report["errors"].append({"stage": stage, "type": type(exc).__name__, "message": "evaluation failed; metrics may be incomplete"})
    finally:
        conn.close()
    report["database_after"] = database_identity(db)
    report["source_after"] = tree_identity(source)
    report["database_unchanged"] = before_db == report["database_after"]
    report["source_unchanged"] = before_source == report["source_after"]
    report["runtime_isolation"] = runtime_isolation(source)
    report["auditors_unchanged"] = all(file_identity(Path(row["path"])) == row for row in report["auditor_identity"].values())
    report["stage_failures"] = stage_failures(report)
    report["evidence_complete"] = (not report["errors"] and report["database_unchanged"] and
                                    report["source_unchanged"] and report["runtime_isolation"]["ok"] and
                                    report["auditors_unchanged"] and not report["stage_failures"] and
                                    set(report["development"]) == set(FIXTURES) and
                                    all(r["fixture_unchanged"] for r in report["development"].values()))
    return report


def compare_reports(baseline: dict, candidate: dict, proofs: list[dict]) -> dict:
    failures = []
    for label, report in (("baseline", baseline), ("candidate", candidate)):
        failures.extend(f"{label}:{failure}" for failure in stage_failures(report))
        if report.get("evidence_complete") is not True:
            failures.append(f"{label}:incomplete_or_unstable_evidence")
        if report.get("output_contract", {}).get("ok") is not True:
            failures.append(f"{label}:output_contract_missing_or_failed")
    same_db = bool(baseline.get("database_before", {}).get("sha256")) and (
        baseline.get("database_before") == candidate.get("database_before"))
    if not same_db:
        failures.append("comparison:database_identity_mismatch")
    development = {}
    for label in FIXTURES:
        left = baseline.get("development", {}).get(label, {})
        right = candidate.get("development", {}).get(label, {})
        identical = bool(left.get("fixture", {}).get("sha256")) and left.get("fixture") == right.get("fixture")
        if not identical:
            failures.append(f"{label}:fixture_identity_mismatch_or_missing")
        before = left.get("evaluation", {}).get("current", {})
        after = right.get("evaluation", {}).get("current", {})
        changes = []
        pairing_ok = False
        if identical:
            try:
                changes = case_changes(before.get("cases", []), after.get("cases", []))
                pairing_ok = True
            except ValueError as exc:
                failures.append(f"{label}:{exc}")
        delta = {k: round(after.get("overall", {}).get(k) - before.get("overall", {}).get(k), 6)
                 if pairing_ok and same_db and isinstance(after.get("overall", {}).get(k), (int, float)) and
                 isinstance(before.get("overall", {}).get(k), (int, float)) else None for k in METRICS}
        development[label] = {"same_fixture": identical, "before": before.get("overall"),
                              "after": after.get("overall"), "delta": delta,
                              "case_pairing_ok": pairing_ok,
                              "case_changes": changes if same_db else []}
    for proof in proofs:
        if not proof["non_empty"]:
            failures.append(f"proof:missing_or_empty:{proof['path']}")
    warnings = []
    regressions = []
    for label, comparison in development.items():
        hit3 = (comparison["after"] or {}).get("hit_at_3")
        if isinstance(hit3, (int, float)) and hit3 < 0.7:
            warnings.append({"dataset": label, "metric": "hit_at_3", "observed": hit3, "review_threshold": 0.7})
        for metric, delta in comparison["delta"].items():
            if delta is not None and delta < 0:
                regressions.append({"dataset": label, "metric": metric, "delta": delta})
    for kind in ("name", "code"):
        before = baseline.get("exact", {}).get(kind, {}).get("overall", {})
        after = candidate.get("exact", {}).get(kind, {}).get("overall", {})
        hit1 = after.get("hit_at_1")
        if isinstance(hit1, (int, float)) and hit1 < 1:
            warnings.append({"dataset": f"exact_{kind}", "metric": "hit_at_1", "observed": hit1, "review_threshold": 1.0})
        if same_db and isinstance(hit1, (int, float)) and isinstance(before.get("hit_at_1"), (int, float)) and hit1 < before["hit_at_1"]:
            regressions.append({"dataset": f"exact_{kind}", "metric": "hit_at_1", "delta": round(hit1 - before["hit_at_1"], 6)})
    return {"schema": "ncs_accuracy_bundle_v1", "generated_at": datetime.now(UTC).isoformat(),
            "evidence_complete": not failures, "failures": failures, "same_database": same_db,
            "quality_assessment": {"status": "not_evaluable" if failures else "regression" if regressions else "review_required" if warnings else "development_checks_no_regression",
                                   "regressions": regressions, "warnings": warnings,
                                   "human_relevance_validated": False, "evidence_completeness_is_accuracy_pass": False},
            "development_comparison": development,
            "source_identity": {label: report.get("source_before") for label, report in (("baseline", baseline), ("candidate", candidate))},
            "source_evidence_generated_at": {label: report.get("generated_at") for label, report in (("baseline", baseline), ("candidate", candidate))},
            "database_identity": baseline.get("database_before") if same_db else None,
            "exact_comparison": {kind: {"before": baseline.get("exact", {}).get(kind, {}).get("overall"),
                                       "after": candidate.get("exact", {}).get(kind, {}).get("overall")}
                                 for kind in ("name", "code")},
            "output_contract_summary": {label: {k: v for k, v in report.get("output_contract", {}).items() if k != "response"}
                                        for label, report in (("baseline", baseline), ("candidate", candidate))},
            "reviewed_transition_summary": {label: {k: v for k, v in report.get("reviewed_transition_sample", {}).items()
                                                     if k != "diagnostic_evaluation"}
                                            for label, report in (("baseline", baseline), ("candidate", candidate))},
            "required_artifacts": proofs, "release_ready": False, "approval_ready": False,
            "db_writes": False, "human_approval_claim": False,
            "limitations": ["Same compact snapshot only; operational DB freshness is not evaluated.",
                            "Exact inventories are structural source self-retrieval; bounded samples are not a census.",
                            "dev90/regression40/dev-long are code-reviewed development expectations, not independent human relevance.",
                            "Stored transition review labels require provenance checks and contain incomplete course labels; no precision claim.",
                            "No hidden holdout inspected; no release/deployment approval; no latency improvement claim."]}


def case_changes(before: list[dict], after: list[dict]) -> list[dict]:
    """Pair in frozen fixture order; query text alone is not a case identity."""
    if not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after):
        raise ValueError("case_pairing:count_or_shape_mismatch")
    changes = []
    for position, (old, new) in enumerate(zip(before, after), start=1):
        if any(not isinstance(row, dict) or not isinstance(row.get("query"), str)
               or not row["query"].strip() for row in (old, new)):
            raise ValueError(f"case_pairing:invalid_identity_at_position:{position}")
        # These are the fixture-derived fields preserved by the existing NL auditor.
        fields = ("case_id", "query", "expected_unit_codes", "category")
        identities = [{key: row[key] for key in fields if key in row} for row in (old, new)]
        if identities[0] != identities[1]:
            raise ValueError(f"case_pairing:identity_or_order_mismatch_at_position:{position}")
        if old.get("first_expected_rank") != new.get("first_expected_rank"):
            changes.append({"fixture_position": position, **identities[0],
                            "before_rank": old.get("first_expected_rank"),
                            "after_rank": new.get("first_expected_rank")})
    return changes


def write_json(path: Path, report: dict):
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, bundle: dict):
    lines = ["# NCS 정확도 증거 묶음", "", "## 판단", "",
             f"- 증거 완전성: {bundle['evidence_complete']}",
             f"- 동일 DB: {bundle['same_database']}",
             f"- 개발셋 품질 신호: {bundle['quality_assessment']['status']} (증거 완전성과 별도)",
             "- 릴리스/사람 승인: 주장하지 않음. 운영 DB 변경 없음.", "", "## 개발셋 비교", "",
             "|셋|기준 Hit@1|현재 Hit@1|기준 Hit@3|현재 Hit@3|MRR 변화|", "|---|---:|---:|---:|---:|---:|"]
    for label, row in bundle["development_comparison"].items():
        before, after = row["before"] or {}, row["after"] or {}
        lines.append(f"|{label}|{before.get('hit_at_1')}|{after.get('hit_at_1')}|{before.get('hit_at_3')}|{after.get('hit_at_3')}|{row['delta']['mrr']}|")
    lines += ["", "개발셋 Hit@3가 0.7 미만인 항목은 검토 경고이며 정확도 통과로 처리하지 않는다.", ""]
    lines += [f"- {row['dataset']}: {row['metric']}={row['observed']} < {row['review_threshold']}" for row in bundle["quality_assessment"]["warnings"]]
    lines += ["", "## 실패 또는 누락", ""] + [f"- {failure}" for failure in bundle["failures"]]
    if not bundle["failures"]:
        lines.append("- 없음. 이 결과는 증거 수집의 완전성만 뜻하며 릴리스 준비 완료가 아니다.")
    lines += ["", "## 범위와 한계", "",
              "개발/회귀셋은 코드 검토 기대값이며 사람의 관련성 판정이 아니다. 전환 리뷰는 근거 패킷을 별도 확인하고 불완전한 과정 라벨로 precision을 주장하지 않는다.", ""]
    lines += [f"- {limitation}" for limitation in bundle["limitations"]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--candidate-source", type=Path, default=ROOT / "src")
    parser.add_argument("--fixture-root", type=Path, default=ROOT)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--per-major-limit", type=int, default=10, help="0 enumerates all distinct source names and codes")
    parser.add_argument("--transition-limit", type=int, default=3)
    parser.add_argument("--required-artifact", action="append", type=Path, default=[])
    parser.add_argument("--skip-plan", action="store_true", help="Bounded checkpoint only; contract remains unverified")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-label", choices=("baseline", "candidate"), help=argparse.SUPPRESS)
    parser.add_argument("--execution-nonce", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.per_major_limit < 0 or args.transition_limit < 0 or args.timeout_seconds < 1:
        parser.error("limits must be nonnegative and timeout positive")
    try:
        db = args.db.resolve(strict=True)
        roots = [args.source_root] if args.worker else [args.baseline_source, args.candidate_source]
        if any(root is None for root in roots):
            raise ValueError("baseline-source is required")
        out = validate_outputs(db, roots, args.out_dir)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    out.mkdir(parents=True, exist_ok=True)
    if args.worker:
        report = worker(args)
        write_json(out / f"{args.worker_label}.json", report)
        return 0 if report["evidence_complete"] else 1
    execution = []
    reports = {}
    for label, source in zip(("baseline", "candidate"), roots):
        nonce = uuid.uuid4().hex
        command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--worker", "--worker-label", label,
                   "--source-root", str(source.resolve()), "--db", str(db), "--out-dir", str(out),
                   "--fixture-root", str(args.fixture_root.resolve()), "--per-major-limit", str(args.per_major_limit),
                   "--transition-limit", str(args.transition_limit), "--execution-nonce", nonce]
        if args.skip_plan:
            command.append("--skip-plan")
        try:
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                       timeout=args.timeout_seconds,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            execution.append({"label": label, "command": command, "exit_code": completed.returncode,
                              "stdout_bytes": len(completed.stdout.encode()), "stderr_bytes": len(completed.stderr.encode())})
            # Do not copy traceback/env-bearing output into reports.
            if completed.returncode in (0, 1) and (out / f"{label}.json").is_file():
                report = json.loads((out / f"{label}.json").read_text(encoding="utf-8"))
                reports[label] = report if report.get("execution_nonce") == nonce else {
                    "evidence_complete": False, "errors": [{"type": "stale_worker_evidence"}]}
            else:
                reports[label] = {"evidence_complete": False, "errors": [{"type": "worker_failed"}]}
        except subprocess.TimeoutExpired:
            execution.append({"label": label, "command": command, "error": "timeout"})
            reports[label] = {"evidence_complete": False, "errors": [{"type": "timeout"}]}
    proofs = artifact_proofs([out / "baseline.json", out / "candidate.json", *args.required_artifact])
    bundle = compare_reports(reports["baseline"], reports["candidate"], proofs)
    bundle["commands_run"] = execution
    bundle["parameters"] = {"per_major_limit": args.per_major_limit, "transition_limit": args.transition_limit,
                            "fixture_root": str(args.fixture_root.resolve()), "skip_plan": args.skip_plan}
    if any(row.get("exit_code", 1) != 0 for row in execution):
        bundle["failures"].append("execution:worker_failed_or_timed_out")
        bundle["evidence_complete"] = False
    write_json(out / "bundle.json", bundle)
    write_markdown(out / "bundle.md", bundle)
    print(json.dumps({"out": str(out / "bundle.json"), "evidence_complete": bundle["evidence_complete"],
                      "failures": bundle["failures"]}, ensure_ascii=False))
    return 0 if bundle["evidence_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
