from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

DEFAULT_REVIEW_ARTIFACT_GLOBS = (
    "*review*.json",
    "*review*.jsonl",
    "*review*.md",
    "*review*.csv",
    "*review*.html",
    "*seedpack*.jsonl",
    "*seedpack*.md",
    "*decision_sheet*.csv",
    "*decision_sheet*.html",
    "*decision_audit*.json",
    "*human_review*.json",
    "*human_review*.md",
)

DEFAULT_EXCLUDE_GLOBS = (
    "*review_artifact_readability*.json",
    "*review_artifact_readability*.md",
)

MOJIBAKE_MARKERS = (
    "\ufffd",
    "?덈",
    "?섎",
    "?몄",
    "?댁",
    "?ㅻ",
    "?쒕",
    "援먯",
    "異붿",
    "吏곷",
    "寃쎌",
    "湲곗",
    "怨듭",
    "媛쒕",
)

DISPLAY_NOISE_METADATA_MARKERS = (
    "possible_encoding_or_display_noise",
    "encoding_display_triage",
)


def _finding(
    *,
    rule_code: str,
    severity: str,
    message: str,
    line: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": rule_code,
        "rule_code": rule_code,
        "severity": severity,
        "message": message,
        "recommended_action": _recommended_action(rule_code),
    }
    if line is not None:
        payload["line"] = line
        payload["line_number"] = line
    payload.update(extra)
    return payload


def _recommended_action(rule_code: str) -> str:
    actions = {
        "missing_artifact": "Verify the artifact path or regenerate the expected report before review.",
        "artifact_path_is_directory": "Pass a concrete review artifact file or use --reports-dir for directory scans.",
        "no_review_artifacts_found": "Verify --reports-dir/--glob inputs or pass explicit --artifact paths.",
        "invalid_utf8": "Regenerate or re-export the artifact as UTF-8/UTF-8-SIG before operator review.",
        "non_utf8_bom_detected": (
            "Re-export the artifact as UTF-8/UTF-8-SIG, for example with Python write_text(..., "
            "encoding='utf-8') or PowerShell Out-File -Encoding utf8."
        ),
        "mojibake_marker_detected": (
            "Route to source/display encoding diagnostics and regenerate the artifact before semantic review."
        ),
        "question_mark_noise_detected": (
            "Inspect the source text and export path; do not use the noisy line as semantic review evidence."
        ),
        "display_noise_triage_metadata_present": (
            "Resolve or isolate encoding_display_triage rows before using the artifact for semantic review."
        ),
    }
    return actions.get(rule_code, "Inspect the artifact before human semantic review.")


def _line_has_question_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    question_count = stripped.count("?")
    return question_count >= 3 and question_count / max(1, len(stripped)) >= 0.08


def _sample(text: str, markers: tuple[str, ...], *, limit: int) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        hit_markers = [marker for marker in markers if marker in line]
        if not hit_markers and not _line_has_question_noise(line):
            continue
        samples.append(
            {
                "line_number": line_number,
                "markers": hit_markers or ["question_mark_noise"],
                "snippet": line.strip()[:240],
            }
        )
        if len(samples) >= limit:
            break
    return samples


def _decode_artifact(raw: bytes) -> tuple[str, str, str | None]:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            text = raw.decode("utf-16")
        except UnicodeDecodeError as exc:
            text = raw.decode("utf-8", errors="replace")
            return text, "utf-16", str(exc)
        return text, "utf-16", "Artifact uses UTF-16 BOM; expected UTF-8 or UTF-8-SIG."
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig", None
        except UnicodeDecodeError as exc:
            return raw.decode("utf-8", errors="replace"), "utf-8-sig", str(exc)
    try:
        return raw.decode("utf-8"), "utf-8", None
    except UnicodeDecodeError as exc:
        return raw.decode("utf-8", errors="replace"), "utf-8", str(exc)


def collect_review_artifact_paths(
    reports_dir: Path,
    *,
    patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    limit: int = 500,
) -> list[Path]:
    globs = patterns or list(DEFAULT_REVIEW_ARTIFACT_GLOBS)
    excludes = exclude_patterns or list(DEFAULT_EXCLUDE_GLOBS)
    paths: dict[str, Path] = {}
    for pattern in globs:
        for path in reports_dir.rglob(pattern):
            if not path.is_file():
                continue
            if any(path.match(exclude) for exclude in excludes):
                continue
            paths[str(path.resolve()).lower()] = path
    ordered = sorted(paths.values(), key=lambda item: str(item).lower())
    if limit > 0:
        return ordered[:limit]
    return ordered


def audit_path(path: Path, *, sample_limit: int = 5) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": str(path),
        "format": path.suffix.lower().lstrip(".") or "unknown",
        "exists": path.exists(),
        "readable": False,
        "encoding": "utf-8",
        "status": "pass",
        "findings": [],
        "sample_lines": [],
    }
    if not path.exists():
        item["status"] = "review_required"
        item["findings"].append(
            _finding(
                rule_code="missing_artifact",
                severity="high",
                message="Artifact path does not exist.",
            )
        )
        return item
    if path.is_dir():
        item["status"] = "review_required"
        item["findings"].append(
            _finding(
                rule_code="artifact_path_is_directory",
                severity="high",
                message="Artifact path is a directory, not a readable review artifact file.",
            )
        )
        return item

    raw = path.read_bytes()
    text, encoding, decode_error = _decode_artifact(raw)
    item.update(
        {
            "readable": decode_error is None,
            "encoding": encoding,
            "byte_count": len(raw),
            "character_count": len(text),
            "line_count": len(text.splitlines()),
            "replacement_char_count": text.count("\ufffd"),
            "question_mark_count": text.count("?"),
        }
    )
    marker_counts = {
        marker: text.count(marker)
        for marker in MOJIBAKE_MARKERS
        if text.count(marker) > 0
    }
    question_noise_lines = sum(1 for line in text.splitlines() if _line_has_question_noise(line))
    metadata_marker_counts = {
        marker: text.count(marker)
        for marker in DISPLAY_NOISE_METADATA_MARKERS
        if text.count(marker) > 0
    }
    item["marker_counts"] = marker_counts
    item["metadata_marker_counts"] = metadata_marker_counts
    item["question_noise_line_count"] = question_noise_lines
    item["sample_lines"] = _sample(text, MOJIBAKE_MARKERS, limit=sample_limit)

    if decode_error is not None and encoding == "utf-16":
        item["findings"].append(
            _finding(
                rule_code="non_utf8_bom_detected",
                severity="high",
                message=decode_error,
            )
        )
    elif decode_error is not None:
        item["findings"].append(
            _finding(
                rule_code="invalid_utf8",
                severity="high",
                message=f"Artifact is not valid UTF-8: {decode_error}",
            )
        )
    if marker_counts:
        item["findings"].append(
            _finding(
                rule_code="mojibake_marker_detected",
                severity="medium",
                message="Artifact contains common Korean mojibake/display-noise markers.",
                marker_counts=marker_counts,
            )
        )
    if question_noise_lines:
        item["findings"].append(
            _finding(
                rule_code="question_mark_noise_detected",
                severity="medium",
                message="Artifact has lines with dense question marks; inspect before human semantic review.",
                line_count=question_noise_lines,
            )
        )
    if metadata_marker_counts:
        item["findings"].append(
            _finding(
                rule_code="display_noise_triage_metadata_present",
                severity="medium",
                message=(
                    "Artifact contains display-noise triage metadata; route these rows to source "
                    "display diagnostics before semantic review."
                ),
                marker_counts=metadata_marker_counts,
            )
        )
    if item["findings"]:
        item["status"] = "review_required"
    return item


def audit_paths(paths: list[Path], *, sample_limit: int = 5) -> dict[str, Any]:
    items = [audit_path(path, sample_limit=sample_limit) for path in paths]
    findings = [
        {**finding, "path": item["path"]}
        for item in items
        for finding in item["findings"]
    ]
    return {
        "schema": "review_artifact_readability_audit_v1",
        "ok": not findings,
        "status": "pass" if not findings else "review_required",
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_decision_required": True,
        "artifact_count": len(items),
        "finding_count": len(findings),
        "findings": findings,
        "artifacts": items,
        "notes": [
            "Report-only display/readability audit for human-review artifacts.",
            "A pass is not approval and must not set human_reviewed, accepted, reviewed, or resolved statuses.",
            "If noise is detected, fix the display/export path or regenerate review artifacts before semantic review.",
            "Do not rewrite ksa_items.ksa_text_raw or treat this audit as a DB write instruction.",
        ],
    }


def build_empty_scan_payload(*, reports_dir: Path, patterns: list[str]) -> dict[str, Any]:
    return {
        "schema": "review_artifact_readability_audit_v1",
        "ok": False,
        "status": "review_required",
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_decision_required": True,
        "artifact_count": 0,
        "finding_count": 1,
        "findings": [
            _finding(
                rule_code="no_review_artifacts_found",
                severity="medium",
                message="No review artifacts matched the supplied scan patterns.",
                reports_dir=str(reports_dir),
                patterns=patterns,
            )
        ],
        "artifacts": [],
        "notes": [
            "Report-only display/readability audit for human-review artifacts.",
            "No files were found; verify the reports directory or pass explicit artifact paths.",
        ],
    }


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Review Artifact Readability Audit",
        "",
        f"- schema: `{payload['schema']}`",
        f"- status: `{payload['status']}`",
        f"- ok: `{str(payload['ok']).lower()}`",
        f"- artifact_count: {payload['artifact_count']}",
        f"- finding_count: {payload['finding_count']}",
        "- report_only: true",
        "- status_update_allowed: false",
        "- db_writes: false",
        "- approval_claim: false",
        "- human_decision_required: true",
        "",
    ]
    scan = payload.get("scan") if isinstance(payload.get("scan"), dict) else {}
    if scan:
        lines.extend(
            [
                "## Scan",
                "",
                f"- explicit_path_count: {scan.get('explicit_path_count')}",
                f"- reports_dir: `{scan.get('reports_dir')}`",
                f"- auto_discovered: `{str(scan.get('auto_discovered')).lower()}`",
                f"- limit: {scan.get('limit')}",
                f"- limit_reached: `{str(scan.get('limit_reached')).lower()}`",
                f"- exclude_patterns: `{scan.get('exclude_patterns')}`",
                "",
            ]
        )
    lines.extend(["## Notes", ""])
    lines.extend(f"- {note}" for note in payload["notes"])
    lines.extend(["", "## Artifacts", ""])
    for item in payload["artifacts"]:
        lines.append(f"### {item['path']}")
        lines.append("")
        lines.append(f"- status: `{item['status']}`")
        lines.append(f"- format: `{item['format']}`")
        lines.append(f"- exists: `{str(item['exists']).lower()}`")
        lines.append(f"- readable_utf8: `{str(item['readable']).lower()}`")
        lines.append(f"- encoding: `{item['encoding']}`")
        if "line_count" in item:
            lines.append(f"- line_count: {item['line_count']}")
            lines.append(f"- replacement_char_count: {item['replacement_char_count']}")
            lines.append(f"- question_noise_line_count: {item['question_noise_line_count']}")
        if item["findings"]:
            lines.append("- findings:")
            for finding in item["findings"]:
                lines.append(f"  - `{finding['rule_code']}` ({finding['severity']}): {finding['message']}")
                lines.append(f"    action: {finding['recommended_action']}")
        if item["sample_lines"]:
            lines.append("- sample_lines:")
            for sample in item["sample_lines"]:
                marker_text = ", ".join(sample["markers"])
                lines.append(
                    f"  - line {sample['line_number']} [{marker_text}]: `{sample['snippet']}`"
                )
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit human-review artifacts for Korean display/readability noise.")
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=ROOT / "reports",
        help="Directory scanned when no explicit paths are supplied.",
    )
    parser.add_argument(
        "--glob",
        action="append",
        dest="globs",
        help="Review artifact glob used with --reports-dir when no explicit paths are supplied. May be repeated.",
    )
    parser.add_argument(
        "--exclude-glob",
        action="append",
        dest="exclude_globs",
        help="Artifact glob excluded during auto-discovery. May be repeated.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Maximum auto-discovered artifacts; <=0 means no limit.")
    parser.add_argument("--sample-limit", type=int, default=5)
    parser.add_argument("--strict", action="store_true", help="Exit nonzero when readability findings are present.")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    patterns = args.globs or list(DEFAULT_REVIEW_ARTIFACT_GLOBS)
    exclude_patterns = args.exclude_globs or list(DEFAULT_EXCLUDE_GLOBS)
    paths = args.paths or collect_review_artifact_paths(
        args.reports_dir,
        patterns=patterns,
        exclude_patterns=exclude_patterns,
        limit=args.limit,
    )
    payload = (
        audit_paths(paths, sample_limit=max(0, args.sample_limit))
        if paths
        else build_empty_scan_payload(reports_dir=args.reports_dir, patterns=patterns)
    )
    payload["scan"] = {
        "explicit_path_count": len(args.paths),
        "reports_dir": str(args.reports_dir),
        "patterns": patterns,
        "exclude_patterns": exclude_patterns,
        "limit": args.limit,
        "limit_reached": bool(not args.paths and args.limit > 0 and len(paths) >= args.limit),
        "auto_discovered": not bool(args.paths),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(args.markdown_out, payload)
    return 1 if args.strict and not payload["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
