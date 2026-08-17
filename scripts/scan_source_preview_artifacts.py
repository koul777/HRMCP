from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BLOCKED_PATH_PARTS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
}
BLOCKED_FILE_NAMES = {
    ".env",
    ".mcp.json",
    "ncs.db",
}
BLOCKED_PREFIXES = (
    ("data", "raw"),
    ("data", "processed"),
    ("reports",),
    ("exports",),
    ("tmp",),
)
SECRET_PATTERNS = {
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |)PRIVATE KEY-----"),
    "env_secret_assignment": re.compile(
        r"(?im)^(?:OPENAI_API_KEY|SERVICE_KEY|NCS_API_KEY|API_KEY|SECRET|TOKEN)\s*=\s*[^#\s].+"
    ),
    "quoted_secret_assignment": re.compile(
        r"(?i)\b(?:secret|token|api_key|service_key)\s*=\s*[\"'][^\"']{4,}[\"']"
    ),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_blocked_path(rel: str) -> str | None:
    parts = tuple(part for part in Path(rel).parts)
    lower_parts = tuple(part.lower() for part in parts)
    if any(part in BLOCKED_PATH_PARTS for part in lower_parts):
        return "blocked workspace/cache path"
    if any(part.endswith(".egg-info") for part in lower_parts):
        return "generated Python package metadata"
    if parts and parts[-1].lower() in BLOCKED_FILE_NAMES:
        return "blocked local secret or generated artifact name"
    for prefix in BLOCKED_PREFIXES:
        if lower_parts[: len(prefix)] == prefix:
            return "blocked generated or local artifact path"
    if rel.endswith(".pyc") or rel.endswith(".pyo"):
        return "python bytecode artifact"
    return None


def _allowed_template_path(rel: str) -> str | None:
    if rel == ".env.example":
        return "allowed environment template"
    return None


def _allowed_secret_example(rel: str, pattern: str) -> str | None:
    if pattern != "quoted_secret_assignment":
        return None
    if rel.startswith("tests/"):
        return "test/smoke/template placeholder"
    if rel == "scripts/mcp_http_health_smoke.py":
        return "test/smoke/template placeholder"
    return None


def _scan_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return None


def scan_tree(output_dir: Path, *, large_file_bytes: int = 5_000_000) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    blocked_name_findings: list[dict[str, Any]] = []
    high_confidence_secret_findings: list[dict[str, Any]] = []
    allowed_template_files: list[dict[str, Any]] = []
    allowed_secret_examples: list[dict[str, Any]] = []
    large_files: list[dict[str, Any]] = []

    for path in sorted(p for p in output_dir.rglob("*") if p.is_file()):
        rel = _relative(path, output_dir)
        blocked_reason = _is_blocked_path(rel)
        if blocked_reason:
            blocked_name_findings.append({"path": rel, "reason": blocked_reason})

        template_reason = _allowed_template_path(rel)
        if template_reason:
            allowed_template_files.append({"path": rel, "reason": template_reason})

        size = path.stat().st_size
        if size > large_file_bytes:
            large_files.append({"path": rel, "size_bytes": size, "limit_bytes": large_file_bytes})

        text = _scan_text(path)
        if text is None:
            continue
        for pattern_name, regex in SECRET_PATTERNS.items():
            for match in regex.finditer(text):
                allowed_reason = _allowed_secret_example(rel, pattern_name)
                line = text.count("\n", 0, match.start()) + 1
                item = {
                    "path": rel,
                    "pattern": pattern_name,
                    "line": line,
                    "redacted_match_prefix": match.group(0)[:12] + "...",
                }
                if allowed_reason:
                    allowed_secret_examples.append(item | {"reason": allowed_reason})
                else:
                    high_confidence_secret_findings.append(item)

    ok = not blocked_name_findings and not high_confidence_secret_findings and not large_files
    return {
        "schema": "ncs_source_preview_secret_artifact_scan_v1",
        "generated_at": _now(),
        "report_only": True,
        "output_dir": str(output_dir),
        "ok": ok,
        "blocked_name_finding_count": len(blocked_name_findings),
        "high_confidence_secret_finding_count": len(high_confidence_secret_findings),
        "allowed_template_file_count": len(allowed_template_files),
        "allowed_secret_example_count": len(allowed_secret_examples),
        "large_file_count": len(large_files),
        "blocked_name_findings": blocked_name_findings,
        "high_confidence_secret_findings": high_confidence_secret_findings,
        "allowed_template_files": allowed_template_files,
        "allowed_secret_examples": allowed_secret_examples,
        "large_files": large_files,
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Source Preview Secret/Artifact Scan",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- output_dir: `{report.get('output_dir')}`",
        f"- blocked_name_finding_count: `{report.get('blocked_name_finding_count')}`",
        f"- high_confidence_secret_finding_count: `{report.get('high_confidence_secret_finding_count')}`",
        f"- large_file_count: `{report.get('large_file_count')}`",
        f"- allowed_template_file_count: `{report.get('allowed_template_file_count')}`",
        f"- allowed_secret_example_count: `{report.get('allowed_secret_example_count')}`",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--large-file-bytes", type=int, default=5_000_000)
    args = parser.parse_args()

    report = scan_tree(args.output_dir, large_file_bytes=args.large_file_bytes)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(report, args.markdown_out)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
