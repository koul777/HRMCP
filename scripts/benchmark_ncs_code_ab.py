"""Compare isolated NCS code revisions against read-only databases.

Workers stay alive but receive calls sequentially, in alternating AB/BA order.
Import/startup, response hashing and IPC are outside the call latency. Each call
opens/closes its connection through the normal read-only DB helper. This is a
local function benchmark; it does not claim HTTP/MCP transport or cold latency.
By default both revisions use one DB. An explicit candidate DB is a separate
artifact comparison and is labeled accordingly in the report.
"""
from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import math
import os
import platform
import pstats
import queue
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LABELS = ("baseline", "candidate")
EXCLUDED_PATHS = ("$.audit.generated_at",)


def sha_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_record(root: Path) -> dict[str, Any]:
    files = {p.relative_to(root).as_posix(): sha_file(p) for p in sorted(root.rglob("*.py"))}
    return {"root": str(root), "files": files, "sha256": hashlib.sha256(
        json.dumps(files, sort_keys=True).encode()).hexdigest()}


def stable_response(value: dict[str, Any]) -> dict[str, Any]:
    value = dict(value)
    if isinstance(value.get("audit"), dict):
        value["audit"] = {k: v for k, v in value["audit"].items() if k != "generated_at"}
    return value


def memory_record() -> dict[str, Any]:
    try:
        import psutil
        info = psutil.Process().memory_info()
        return {"rss_bytes": info.rss, "peak_rss_bytes": getattr(info, "peak_wset", None),
                "peak_source": "Windows PeakWorkingSetSize" if hasattr(info, "peak_wset") else None}
    except ImportError:
        return {"rss_bytes": None, "peak_rss_bytes": None, "peak_source": None}


def worker(source: Path, db: Path) -> int:
    sys.path.insert(0, str(source))
    from ncs_mcp import server
    from ncs_mcp.db import connect

    @contextmanager
    def open_db():
        conn = connect(db, read_only=True)
        try:
            yield conn
        finally:
            conn.close()

    server.open_db = open_db
    with open_db() as conn:
        pragmas = {key: conn.execute(f"PRAGMA {key}").fetchone()[0]
                   for key in ("query_only", "mmap_size", "cache_size", "foreign_keys", "busy_timeout")}
    print(json.dumps({"ready": True, "source": server.__file__, "pragmas": pragmas,
                      "python": sys.version, "sqlite": sqlite3.sqlite_version, **memory_record()}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("stop"):
            return 0
        try:
            case = request["case"]
            fn = server.search_ncs if case["workload"].startswith("search") else server.resolve_ncs_query_scope
            profiler = cProfile.Profile() if request.get("profile") else None
            if profiler:
                profiler.enable()
            started = time.perf_counter()
            result = fn(**case["params"])
            elapsed = (time.perf_counter() - started) * 1000
            if profiler:
                profiler.disable()
            stable = stable_response(result)
            body = json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
            response = {"elapsed_ms": round(elapsed, 4), "sha256": hashlib.sha256(body).hexdigest(),
                        "bytes": len(body), "returned": result.get("returned", len(result.get("candidates", []))),
                        "ok": result.get("ok"), **memory_record()}
            if request.get("include_body"):
                response["body"] = stable
            if profiler:
                stats = pstats.Stats(profiler)
                response["profile"] = [
                    {"file": key[0], "line": key[1], "function": key[2], "primitive_calls": val[0],
                     "calls": val[1], "self_ms": round(val[2] * 1000, 3), "cumulative_ms": round(val[3] * 1000, 3)}
                    for key, val in sorted(stats.stats.items(), key=lambda item: -item[1][3])[:100]
                ]
            print(json.dumps(response, ensure_ascii=False), flush=True)
        except Exception:
            print(json.dumps({"error": traceback.format_exc()}), flush=True)
    return 0


class Worker:
    def __init__(self, source: Path, db: Path, log: Path):
        self.log = log.open("w", encoding="utf-8")
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
               "NCS_DB_PATH": str(db), "NCS_MCP_READ_ONLY": "1", "NCS_MCP_ENABLE_OPERATOR_TOOLS": "0"}
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", "--source", str(source), "--db", str(db)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, encoding="utf-8", env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.ready = self.receive()
            if not self.ready.get("ready"):
                raise RuntimeError(self.ready)
        except BaseException:
            self.close()
            raise

    def _read(self):
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put("")

    def receive(self):
        try:
            line = self.lines.get(timeout=120)
        except queue.Empty as exc:
            raise TimeoutError("Benchmark worker exceeded the 120-second per-call bound") from exc
        if not line:
            raise RuntimeError(f"Worker exited; inspect {self.log.name}")
        result = json.loads(line)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result

    def call(self, case, **options):
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"case": case, **options}, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        return self.receive()

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()
        self.log.close()


def summarize(records):
    output = {}
    for group in sorted({r["workload"] for r in records}):
        subset = [r for r in records if r["workload"] == group]
        medians = {label: statistics.median(r["p50_ms"][label] for r in subset) for label in LABELS}
        p95 = {}
        for label in LABELS:
            pool = sorted(sample["elapsed_ms"] for r in subset for sample in r["samples"][label])
            p95[label] = pool[math.ceil(len(pool) * .95) - 1]
        output[group] = {"cases": len(subset), "query_p50_median_ms": medians, "pooled_p95_ms": p95,
                         "p50_reduction_percent": round((1 - medians["candidate"] / medians["baseline"]) * 100, 3),
                         "regression_over_5pct": [r["id"] for r in subset if r["p50_ms"]["candidate"] > r["p50_ms"]["baseline"] * 1.05],
                         "all_responses_equal": all(r["response_equal"] for r in subset)}
    return output


def benchmark(args):
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.workload:
        cases = [c for c in cases if c["workload"] in args.workload]
    if args.case_id:
        cases = [c for c in cases if c["id"] in args.case_id]
    if not cases:
        raise ValueError("No benchmark cases selected")
    if not 1 <= args.runs <= 30:
        raise ValueError("runs must be 1..30 (use at least 7 for final latency gates)")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    sources = {"baseline": args.baseline_source.resolve(strict=True), "candidate": args.candidate_source.resolve(strict=True)}
    db = args.db.resolve(strict=True)
    candidate_db = (args.candidate_db or args.db).resolve(strict=True)
    databases = {"baseline": db, "candidate": candidate_db}
    before = {label: source_record(path) for label, path in sources.items()}
    report = {"schema": "ncs_code_ab_v1", "started_at": datetime.now(UTC).isoformat(),
              "mode": "separate_process_interleaved_read_only_function_calls",
              "platform": platform.platform(), "runs_per_case": args.runs, "warmups_per_case": 1,
              "excluded_response_paths": EXCLUDED_PATHS, "source_before": before,
              "database": {"path": str(db), "bytes": db.stat().st_size, "sha256_before": sha_file(db)},
              "same_database": db == candidate_db,
              "candidate_database": {"path": str(candidate_db), "bytes": candidate_db.stat().st_size,
                                     "sha256_before": sha_file(candidate_db)},
              "cases_sha256": sha_file(args.cases), "records": [], "deployment_performed": False}
    workers = {}
    try:
        for label in LABELS:
            workers[label] = Worker(sources[label], databases[label], args.out.with_suffix(f".{label}.stderr.log"))
        report["workers"] = {k: v.ready for k, v in workers.items()}
        for case_index, case in enumerate(cases):
            warm = {label: workers[label].call(case, include_body=True, profile=args.profile) for label in LABELS}
            samples = {label: [] for label in LABELS}
            for run in range(args.runs):
                for label in LABELS if (run + case_index) % 2 == 0 else tuple(reversed(LABELS)):
                    samples[label].append(workers[label].call(case))
            hashes = {label: {r["sha256"] for r in samples[label]} | {warm[label]["sha256"]} for label in LABELS}
            record = {**case, "samples": samples, "warmup": warm,
                      "p50_ms": {label: statistics.median(r["elapsed_ms"] for r in samples[label]) for label in LABELS},
                      "response_equal": len(hashes["baseline"]) == len(hashes["candidate"]) == 1 and hashes["baseline"] == hashes["candidate"]}
            report["records"].append(record)
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"case": case["id"], "equal": record["response_equal"], "p50_ms": record["p50_ms"]}), flush=True)
    finally:
        for item in workers.values():
            item.close()
    report["source_after"] = {label: source_record(path) for label, path in sources.items()}
    report["source_unchanged_during_run"] = report["source_before"] == report["source_after"]
    report["database"]["sha256_after"] = sha_file(db)
    report["database"]["unchanged"] = report["database"]["sha256_before"] == report["database"]["sha256_after"]
    report["candidate_database"]["sha256_after"] = sha_file(candidate_db)
    report["candidate_database"]["unchanged"] = report["candidate_database"]["sha256_before"] == report["candidate_database"]["sha256_after"]
    report["summary"] = summarize(report["records"])
    report["all_responses_equal"] = all(r["response_equal"] for r in report["records"])
    report["finished_at"] = datetime.now(UTC).isoformat()
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(args.out), "summary": report["summary"]}, ensure_ascii=False), flush=True)
    return 0 if report["all_responses_equal"] and report["database"]["unchanged"] and report["candidate_database"]["unchanged"] and report["source_unchanged_during_run"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--candidate-db", type=Path, help="Optional artifact comparison; omit for same-DB code A/B")
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--candidate-source", type=Path, default=ROOT / "src")
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--workload", action="append")
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--profile", action="store_true", help="cProfile warmup only; measured samples stay uninstrumented")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.worker:
        return worker(args.source.resolve(strict=True), args.db.resolve(strict=True))
    if not all((args.baseline_source, args.cases, args.out)):
        parser.error("--baseline-source, --cases and --out are required")
    return benchmark(args)


if __name__ == "__main__":
    raise SystemExit(main())
