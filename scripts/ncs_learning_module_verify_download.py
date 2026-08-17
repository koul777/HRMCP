from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from ncs_learning_module_pdf_download import _is_pdf


SCHEMA = "ncs_learning_module_pdf_download_verification_v1"


def _iter_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if not path or not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _class_code_from_name(path: Path) -> str:
    return path.name.split("_", 1)[0]


def _scope_files(out_dir: Path, scope_prefix: str | None) -> list[Path]:
    files = sorted(out_dir.glob("*.pdf"))
    if not scope_prefix:
        return files
    return [path for path in files if _class_code_from_name(path).startswith(scope_prefix)]


def verify_download(
    *,
    out_dir: Path,
    manifest_jsonl: Path | None = None,
    scope_prefix: str | None = None,
) -> dict[str, Any]:
    pdfs = _scope_files(out_dir, scope_prefix)
    headers_json = sorted(out_dir.glob("*.headers.json"))
    headers_tmp = sorted(out_dir.glob("*.headers.tmp"))
    if scope_prefix:
        headers_json = [path for path in headers_json if _class_code_from_name(path).startswith(scope_prefix)]
        headers_tmp = [path for path in headers_tmp if _class_code_from_name(path).startswith(scope_prefix)]
    bad_pdf_paths = [str(path) for path in pdfs if not _is_pdf(path)]
    prefix_counts_2 = Counter(_class_code_from_name(path)[:2] for path in pdfs)
    prefix_counts_4 = Counter(_class_code_from_name(path)[:4] for path in pdfs)
    prefix_counts_6 = Counter(_class_code_from_name(path)[:6] for path in pdfs)
    manifest_rows = _iter_jsonl(manifest_jsonl)
    manifest_status_counts = Counter(str(row.get("status") or "unknown") for row in manifest_rows)
    total_bytes = sum(path.stat().st_size for path in pdfs if path.exists())
    return {
        "schema": SCHEMA,
        "out_dir": str(out_dir),
        "manifest_jsonl": str(manifest_jsonl) if manifest_jsonl else None,
        "scope_prefix": scope_prefix,
        "pdf_count": len(pdfs),
        "headers_json_count": len(headers_json),
        "headers_tmp_count": len(headers_tmp),
        "total_bytes": total_bytes,
        "total_gib": round(total_bytes / (1024**3), 3),
        "bad_pdf_magic_count": len(bad_pdf_paths),
        "bad_pdf_magic_paths": bad_pdf_paths,
        "prefix_counts_2": dict(sorted(prefix_counts_2.items())),
        "prefix_counts_4": dict(sorted(prefix_counts_4.items())),
        "prefix_counts_6": dict(sorted(prefix_counts_6.items())),
        "manifest_row_count": len(manifest_rows),
        "manifest_status_counts": dict(sorted(manifest_status_counts.items())),
        "ok": len(bad_pdf_paths) == 0 and len(headers_tmp) == 0,
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# NCS Learning Module PDF Download Verification",
        "",
        f"- out_dir: {report['out_dir']}",
        f"- scope_prefix: {report['scope_prefix']}",
        f"- pdf_count: {report['pdf_count']}",
        f"- headers_json_count: {report['headers_json_count']}",
        f"- headers_tmp_count: {report['headers_tmp_count']}",
        f"- total_gib: {report['total_gib']}",
        f"- bad_pdf_magic_count: {report['bad_pdf_magic_count']}",
        f"- ok: {str(report['ok']).lower()}",
        "",
        "## Manifest Status Counts",
        "",
    ]
    for status, count in report["manifest_status_counts"].items():
        lines.append(f"- {status}: {count}")
    lines.extend(["", "## Prefix Counts 6", ""])
    for prefix, count in report["prefix_counts_6"].items():
        lines.append(f"- {prefix}: {count}")
    if report["bad_pdf_magic_paths"]:
        lines.extend(["", "## Bad PDF Magic Paths", ""])
        for bad_path in report["bad_pdf_magic_paths"]:
            lines.append(f"- {bad_path}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--manifest-jsonl", type=Path)
    parser.add_argument("--scope-prefix")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args()
    report = verify_download(
        out_dir=args.out_dir,
        manifest_jsonl=args.manifest_jsonl,
        scope_prefix=args.scope_prefix,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(report, args.markdown_out)
    print(json.dumps({"out": str(args.out), "markdown_out": str(args.markdown_out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
