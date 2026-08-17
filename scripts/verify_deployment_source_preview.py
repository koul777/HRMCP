from __future__ import annotations

import argparse
import hashlib
import json
import tokenize
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scan_source_preview_artifacts import _is_blocked_path
except ModuleNotFoundError:  # pragma: no cover - package-style test import
    from scripts.scan_source_preview_artifacts import _is_blocked_path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_target(root: Path, relative_path: str) -> Path | None:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _compile_python(path: Path) -> str | None:
    try:
        with tokenize.open(path) as handle:
            source = handle.read()
        compile(source, str(path), "exec")
    except (OSError, SyntaxError, UnicodeError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def verify_preview_tree(
    source_preview_export: dict[str, Any],
    *,
    source_preview_export_path: Path | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    configured_output = output_dir or Path(str(source_preview_export.get("output_dir") or ""))
    resolved_output = configured_output.resolve()
    expected_rows = list(source_preview_export.get("copied_files") or [])
    expected_paths = {
        str(item.get("path") or "").replace("\\", "/")
        for item in expected_rows
        if item.get("path")
    }

    missing_required: list[dict[str, str]] = []
    hash_mismatches: list[dict[str, Any]] = []
    size_mismatches: list[dict[str, Any]] = []
    invalid_expected_paths: list[dict[str, str]] = []
    compile_errors: list[dict[str, str]] = []

    actual_files = (
        sorted(path for path in resolved_output.rglob("*") if path.is_file())
        if resolved_output.is_dir()
        else []
    )
    actual_paths = {
        path.relative_to(resolved_output).as_posix()
        for path in actual_files
    }

    for row in expected_rows:
        relative_path = str(row.get("path") or "").replace("\\", "/")
        target = _safe_target(resolved_output, relative_path)
        if not relative_path or target is None:
            invalid_expected_paths.append(
                {"path": relative_path, "reason": "invalid or escaping expected path"}
            )
            continue
        if not target.is_file():
            missing_required.append({"path": relative_path, "reason": "file missing"})
            continue
        expected_size = row.get("bytes")
        actual_size = target.stat().st_size
        if expected_size is not None and int(expected_size) != actual_size:
            size_mismatches.append(
                {
                    "path": relative_path,
                    "expected_bytes": int(expected_size),
                    "actual_bytes": actual_size,
                }
            )
        expected_hash = str(row.get("sha256") or "").lower()
        actual_hash = _sha256(target)
        if not expected_hash or expected_hash != actual_hash:
            hash_mismatches.append(
                {
                    "path": relative_path,
                    "expected_sha256": expected_hash or None,
                    "actual_sha256": actual_hash,
                }
            )

    extra_files = sorted(actual_paths - expected_paths)
    blocked_paths = []
    for relative_path in sorted(actual_paths):
        reason = _is_blocked_path(relative_path)
        if reason:
            blocked_paths.append({"path": relative_path, "reason": reason})

    for path in actual_files:
        if path.suffix.lower() != ".py":
            continue
        error = _compile_python(path)
        if error:
            compile_errors.append(
                {"path": path.relative_to(resolved_output).as_posix(), "error": error}
            )

    source_export_ok = source_preview_export.get("ok") is True
    output_dir_ok = resolved_output.is_dir()
    ok = bool(
        source_export_ok
        and output_dir_ok
        and expected_rows
        and not missing_required
        and not hash_mismatches
        and not size_mismatches
        and not invalid_expected_paths
        and not extra_files
        and not blocked_paths
        and not compile_errors
    )
    return {
        "schema": "ncs_deployment_source_preview_tree_verification_v1",
        "generated_at": _now(),
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "ok": ok,
        "output_dir": str(resolved_output),
        "source_preview_export": (
            str(source_preview_export_path) if source_preview_export_path else None
        ),
        "source_preview_export_ok": source_export_ok,
        "output_dir_exists": resolved_output.exists(),
        "output_dir_is_dir": output_dir_ok,
        "file_count": len(actual_files),
        "expected_file_count": len(expected_rows),
        "hash_mismatch_count": len(hash_mismatches),
        "size_mismatch_count": len(size_mismatches),
        "missing_required_count": len(missing_required),
        "extra_file_count": len(extra_files),
        "invalid_expected_path_count": len(invalid_expected_paths),
        "summary": {
            "blocked_path_count": len(blocked_paths),
            "compile_error_count": len(compile_errors),
        },
        "missing_required": missing_required,
        "hash_mismatches": hash_mismatches,
        "size_mismatches": size_mismatches,
        "extra_files": extra_files,
        "invalid_expected_paths": invalid_expected_paths,
        "blocked_paths": blocked_paths,
        "compile_errors": compile_errors,
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Deployment Source Preview Tree Verification",
        "",
        f"- ok: `{str(report.get('ok')).lower()}`",
        f"- output_dir: `{report.get('output_dir')}`",
        f"- file_count: `{report.get('file_count')}`",
        f"- expected_file_count: `{report.get('expected_file_count')}`",
        f"- hash_mismatch_count: `{report.get('hash_mismatch_count')}`",
        f"- size_mismatch_count: `{report.get('size_mismatch_count')}`",
        f"- missing_required_count: `{report.get('missing_required_count')}`",
        f"- extra_file_count: `{report.get('extra_file_count')}`",
        f"- blocked_path_count: `{report.get('summary', {}).get('blocked_path_count')}`",
        f"- compile_error_count: `{report.get('summary', {}).get('compile_error_count')}`",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a deployment source-preview tree against its export manifest."
    )
    parser.add_argument("--source-preview-export", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args()

    source_export = json.loads(args.source_preview_export.read_text(encoding="utf-8"))
    report = verify_preview_tree(
        source_export,
        source_preview_export_path=args.source_preview_export,
        output_dir=args.output_dir,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(payload + "\n", encoding="utf-8")
    if args.markdown_out:
        write_markdown(report, args.markdown_out)
    print(payload)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
