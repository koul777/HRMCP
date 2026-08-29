"""Build a verified, deployable compact NCS ontology snapshot without deploying it.

This command deliberately orchestrates only the existing local export, package,
and archive verification stages.  It neither calls NCS APIs nor changes the
canonical source database or any human-review status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
SQLITE_HEADER = b"SQLite format 3\x00"
REPORT_SCHEMA = "ncs_vercel_snapshot_build_report_v1"
STDIO_TAIL_BYTES = 16_384


class SnapshotBuildError(RuntimeError):
    """Raised when a build cannot safely continue."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _tail(stream: Any) -> dict[str, Any]:
    stream.flush()
    size = stream.seek(0, 2)
    start = max(size - STDIO_TAIL_BYTES, 0)
    stream.seek(start)
    return {
        "tail": stream.read().decode("utf-8", errors="replace"),
        "bytes": size,
        "truncated": size > STDIO_TAIL_BYTES,
    }


def _artifact(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SnapshotBuildError(f"required artifact is missing: {path}")
    size_bytes = path.stat().st_size
    if size_bytes <= 0:
        raise SnapshotBuildError(f"required artifact is empty: {path}")
    return {
        "path": str(path),
        "bytes": size_bytes,
        "sha256": _sha256(path),
    }


def _source_artifact(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SnapshotBuildError(f"source SQLite file does not exist: {path}")
    with path.open("rb") as stream:
        header = stream.read(len(SQLITE_HEADER))
    if header != SQLITE_HEADER:
        raise SnapshotBuildError(f"source SQLite header is invalid: {path}")
    result = _artifact(path)
    result["sqlite_header_valid"] = True
    return result


def _resolved_output_path(value: Path) -> Path:
    return value.expanduser().resolve(strict=False)


def _validate_paths(
    source: Path,
    output_db: Path,
    archive: Path,
    manifest: Path,
    report: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    resolved_source = source.expanduser().resolve(strict=True)
    outputs = tuple(_resolved_output_path(value) for value in (output_db, archive, manifest, report))
    names = ("output DB", "archive", "manifest", "report")
    if len(set(outputs)) != len(outputs):
        raise SnapshotBuildError("output DB, archive, manifest, and report paths must be distinct")
    for name, output in zip(names, outputs):
        if output == resolved_source:
            raise SnapshotBuildError(f"{name} must not be the source SQLite path")
        if output.exists():
            raise SnapshotBuildError(
                f"{name} already exists; refusing to replace an existing output: {output}"
            )
    return (resolved_source, *outputs)


def build_plan(
    source: Path,
    output_db: Path,
    archive: Path,
    manifest: Path,
) -> list[dict[str, Any]]:
    """Return the fixed, shell-free argv plan used for every build."""
    return [
        {
            "name": "export_compact_snapshot",
            "argv": [
                sys.executable,
                str(ROOT / "scripts" / "export_interview_serving_db.py"),
                "--source",
                str(source),
                "--destination",
                str(output_db),
                "--profile",
                "vercel-ontology-compact",
            ],
            "required_artifacts": [str(output_db)],
        },
        {
            "name": "package_compact_snapshot",
            "argv": [
                sys.executable,
                str(ROOT / "scripts" / "package_vercel_compact_snapshot.py"),
                "--database",
                str(output_db),
                "--archive",
                str(archive),
                "--manifest",
                str(manifest),
            ],
            "required_artifacts": [str(archive), str(manifest)],
        },
        {
            "name": "verify_archive_only",
            "argv": [
                sys.executable,
                str(ROOT / "scripts" / "verify_vercel_compact_package.py"),
                "--archive",
                str(archive),
                "--manifest",
                str(manifest),
                "--skip-function-bundle-check",
            ],
            "required_artifacts": [str(archive), str(manifest)],
        },
    ]


def _run_stage(stage: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        completed = subprocess.run(
            stage["argv"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            check=False,
            shell=False,
        )
        stdout_record = _tail(stdout)
        stderr_record = _tail(stderr)
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        **stage,
        "duration_ms": duration_ms,
        "returncode": completed.returncode,
        "stdout": stdout_record,
        "stderr": stderr_record,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_snapshot(
    *,
    source: Path,
    output_db: Path,
    archive: Path,
    manifest: Path,
    report_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build the fixed compact snapshot pipeline and return its audit report."""
    source, output_db, archive, manifest, report_path = _validate_paths(
        source, output_db, archive, manifest, report_path
    )
    source_record = _source_artifact(source)
    plan = build_plan(source, output_db, archive, manifest)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "ok": False,
        "dry_run": dry_run,
        "source": source_record,
        "outputs": {
            "database": str(output_db),
            "archive": str(archive),
            "manifest": str(manifest),
            "report": str(report_path),
        },
        "stages": [],
        "artifacts": {},
        "policy": {
            "source_database_mutated": False,
            "api_collection_called": False,
            "human_review_statuses_changed": False,
            "deployment_performed": False,
            "verification_scope": "archive_only",
        },
    }
    if dry_run:
        report["stages"] = plan
        report["ok"] = True
        return report

    expected_artifacts = {
        "export_compact_snapshot": (output_db,),
        "package_compact_snapshot": (archive, manifest),
        "verify_archive_only": (archive, manifest),
    }
    for stage in plan:
        stage_record = _run_stage(stage)
        report["stages"].append(stage_record)
        if stage_record["returncode"] != 0:
            report["error"] = {
                "stage": stage["name"],
                "message": f"stage returned nonzero exit code {stage_record['returncode']}",
            }
            return report
        try:
            for artifact_path in expected_artifacts[stage["name"]]:
                _artifact(artifact_path)
        except SnapshotBuildError as exc:
            report["error"] = {"stage": stage["name"], "message": str(exc)}
            return report

    try:
        report["artifacts"] = {
            "source": source_record,
            "database": _artifact(output_db),
            "archive": _artifact(archive),
            "manifest": _artifact(manifest),
        }
        with output_db.open("rb") as stream:
            report["artifacts"]["database"]["sqlite_header_valid"] = (
                stream.read(len(SQLITE_HEADER)) == SQLITE_HEADER
            )
        if not report["artifacts"]["database"]["sqlite_header_valid"]:
            raise SnapshotBuildError("exported snapshot SQLite header is invalid")
    except SnapshotBuildError as exc:
        report["error"] = {"stage": "final_artifact_validation", "message": str(exc)}
        return report
    report["ok"] = True
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="canonical input NCS SQLite database")
    parser.add_argument("--output-db", type=Path, required=True, help="new compact SQLite snapshot path")
    parser.add_argument("--archive", type=Path, required=True, help="new compact snapshot ZIP path")
    parser.add_argument("--manifest", type=Path, required=True, help="new compact snapshot manifest path")
    parser.add_argument("--report", type=Path, required=True, help="new JSON build report path")
    parser.add_argument("--dry-run", action="store_true", help="print the exact argv plan without execution or writes")
    args = parser.parse_args(argv)
    try:
        report = build_snapshot(
            source=args.source,
            output_db=args.output_db,
            archive=args.archive,
            manifest=args.manifest,
            report_path=args.report,
            dry_run=args.dry_run,
        )
    except (OSError, SnapshotBuildError) as exc:
        parser.error(str(exc))
    if not args.dry_run:
        try:
            _write_report(_resolved_output_path(args.report), report)
        except OSError as exc:
            print(json.dumps({**report, "report_write_error": str(exc)}, ensure_ascii=False, indent=2))
            return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
