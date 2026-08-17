from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any


SCHEMA = "ncs_learning_module_download_assessment_v1"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _normalized_class_code(raw: str | None) -> str:
    if not raw:
        return ""
    return raw.split("_", 1)[0].strip()


def _estimate_storage(unique_count: int | None, sample_bytes: int | None) -> dict[str, Any]:
    if not unique_count or not sample_bytes:
        return {"sample_bytes": sample_bytes, "estimated_gib": None}
    return {
        "sample_bytes": sample_bytes,
        "estimated_bytes": int(unique_count * sample_bytes),
        "estimated_gib": round((unique_count * sample_bytes) / (1024**3), 2),
    }


def _runtime_estimate(unique_count: int | None) -> dict[str, Any]:
    if not unique_count:
        return {}
    return {
        "at_2_seconds_per_file_hours": round((unique_count * 2) / 3600, 2),
        "at_5_seconds_per_file_hours": round((unique_count * 5) / 3600, 2),
        "at_10_seconds_per_file_hours": round((unique_count * 10) / 3600, 2),
    }


def _disk_usage(path: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(path.resolve().anchor or path.resolve().parent)
    return {
        "total_gib": round(usage.total / (1024**3), 2),
        "used_gib": round(usage.used / (1024**3), 2),
        "free_gib": round(usage.free / (1024**3), 2),
    }


def assess_download(
    *,
    index_jsonl: Path,
    probe_json: Path,
    summary_json: Path | None,
) -> dict[str, Any]:
    rows = _iter_jsonl(index_jsonl)
    probe = _read_json(probe_json)
    summary = _read_json(summary_json) if summary_json else {}

    unique_keys = {str(row.get("download_key")) for row in rows if row.get("download_key")}
    page_indexes = [int(row["page_index"]) for row in rows if row.get("page_index") is not None]
    max_page_index_observed = (
        summary.get("max_page_index_observed")
        or probe.get("max_page_index_observed")
        or probe.get("page_index", {}).get("page_index_max_observed")
        or probe.get("observed_page_index", {}).get("page_index_max_observed")
    )
    possible_page_count = int(max_page_index_observed) + 1 if max_page_index_observed is not None else None
    observed_pages = len(set(page_indexes))
    observed_complete = bool(possible_page_count and observed_pages >= possible_page_count)
    rows_per_page = round(len(rows) / observed_pages, 2) if observed_pages else None
    unique_ratio = round(len(unique_keys) / len(rows), 4) if rows else None
    if observed_complete:
        estimated_raw_rows = len(rows)
        estimated_unique_files = len(unique_keys)
    else:
        estimated_raw_rows = int(possible_page_count * rows_per_page) if possible_page_count and rows_per_page else None
        estimated_unique_files = (
            int(round(estimated_raw_rows * unique_ratio)) if estimated_raw_rows and unique_ratio is not None else None
        )
    sample_bytes = (
        probe.get("sample_download", {}).get("bytes")
        or probe.get("sample_download", {}).get("pdf_probe", {}).get("bytes")
        or probe.get("sample_bytes")
    )

    major_counts: Counter[str] = Counter()
    small_counts: Counter[str] = Counter()
    hr_rows = 0
    hr_unique_keys: set[str] = set()
    for row in rows:
        code = _normalized_class_code(row.get("ncs_cl_cd"))
        if not code:
            continue
        major = code[:2]
        small = code[:6]
        major_counts[major] += 1
        small_counts[small] += 1
        if code.startswith("02"):
            hr_rows += 1
            if row.get("download_key"):
                hr_unique_keys.add(str(row["download_key"]))

    can_download_all = True
    recommended_now = False
    rationale = [
        "The official file-search surface exposes resumable download keys, so full download is technically possible.",
        "The sample PDF is image-heavy, so content use will likely require selective OCR after download.",
        "Study modules are currently a legacy/reference source and should not feed active recommendation scoring by default.",
        "The first safe phase is complete index crawl and deduplication; bulk PDF download should be a separate, throttled job.",
    ]
    if estimated_unique_files and sample_bytes:
        estimated_gib = (estimated_unique_files * sample_bytes) / (1024**3)
        if estimated_gib >= 25:
            rationale.append("Storage estimate is large enough that downloading all PDFs should require an explicit storage/runtime decision.")
    storage_estimate = _estimate_storage(estimated_unique_files, sample_bytes)
    disk = _disk_usage(index_jsonl.parent)
    if storage_estimate.get("estimated_gib") is not None:
        storage_estimate["current_drive_free_gib"] = disk["free_gib"]
        storage_estimate["fits_current_drive"] = storage_estimate["estimated_gib"] < disk["free_gib"]

    return {
        "schema": SCHEMA,
        "index_jsonl": str(index_jsonl),
        "probe_json": str(probe_json),
        "summary_json": str(summary_json) if summary_json else None,
        "observed": {
            "row_count": len(rows),
            "observed_pages": observed_pages,
            "observed_complete": observed_complete,
            "max_seen_page_index_in_jsonl": max(page_indexes) if page_indexes else None,
            "unique_download_key_count": len(unique_keys),
            "unique_ratio": unique_ratio,
            "rows_per_page": rows_per_page,
            "major_counts": dict(sorted(major_counts.items())),
            "top_small_class_counts": dict(small_counts.most_common(20)),
            "hr_major_02_rows": hr_rows,
            "hr_major_02_unique_download_key_count": len(hr_unique_keys),
        },
        "estimated_full_scope": {
            "possible_page_count": possible_page_count,
            "estimated_raw_rows": estimated_raw_rows,
            "estimated_unique_files": estimated_unique_files,
            "storage_if_unique_files": storage_estimate,
            "runtime_if_unique_files": _runtime_estimate(estimated_unique_files),
            "current_drive": disk,
        },
        "decision": {
            "can_download_all": can_download_all,
            "recommended_now": recommended_now,
            "recommended_sequence": [
                "finish_full_index",
                "deduplicate_download_keys",
                "download_hr_major_02_or_requested_scope_first",
                "hash_and_record_pdf_metadata",
                "run_selective_ocr_for_human_review_scope",
                "only_then_consider_full_pdf_mirror",
            ],
            "active_recommendation_scoring": False,
            "allowed_role": "auxiliary_review_reference",
            "rationale": rationale,
        },
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    observed = report["observed"]
    estimate = report["estimated_full_scope"]
    decision = report["decision"]
    storage = estimate["storage_if_unique_files"]
    runtime = estimate["runtime_if_unique_files"]
    drive = estimate["current_drive"]
    lines = [
        "# NCS Learning Module Download Assessment",
        "",
        "## Answer",
        "",
        f"- Can download all files technically: {str(decision['can_download_all']).lower()}",
        f"- Recommended to start full PDF download now: {str(decision['recommended_now']).lower()}",
        f"- Active recommendation scoring: {str(decision['active_recommendation_scoring']).lower()}",
        f"- Allowed role: {decision['allowed_role']}",
        "",
        "## Current Index Evidence",
        "",
        f"- JSONL rows observed: {observed['row_count']}",
        f"- Pages observed: {observed['observed_pages']}",
        f"- Index complete: {str(observed['observed_complete']).lower()}",
        f"- Max page index seen in JSONL: {observed['max_seen_page_index_in_jsonl']}",
        f"- Unique download keys observed: {observed['unique_download_key_count']}",
        f"- Unique key ratio: {observed['unique_ratio']}",
        f"- HR major 02 rows observed: {observed['hr_major_02_rows']}",
        f"- HR major 02 unique keys observed: {observed['hr_major_02_unique_download_key_count']}",
        "",
        "## Full Scope Estimate",
        "",
        f"- Possible page count: {estimate['possible_page_count']}",
        f"- Estimated raw rows: {estimate['estimated_raw_rows']}",
        f"- Estimated unique files: {estimate['estimated_unique_files']}",
        f"- Sample bytes per PDF: {storage['sample_bytes']}",
        f"- Estimated storage GiB: {storage['estimated_gib']}",
        f"- Current drive free GiB: {drive['free_gib']}",
        f"- Fits current drive: {str(storage.get('fits_current_drive')).lower()}",
        f"- Runtime at 2 sec/file hours: {runtime.get('at_2_seconds_per_file_hours')}",
        f"- Runtime at 5 sec/file hours: {runtime.get('at_5_seconds_per_file_hours')}",
        f"- Runtime at 10 sec/file hours: {runtime.get('at_10_seconds_per_file_hours')}",
        "",
        "## Recommended Sequence",
        "",
    ]
    for item in decision["recommended_sequence"]:
        lines.append(f"- {item}")
    lines.extend(["", "## Rationale", ""])
    for item in decision["rationale"]:
        lines.append(f"- {item}")
    lines.extend(["", "## Major Counts In Observed Index", ""])
    for code, count in observed["major_counts"].items():
        lines.append(f"- {code}: {count}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index-jsonl", type=Path, required=True)
    parser.add_argument("--probe-json", type=Path, required=True)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args()
    report = assess_download(
        index_jsonl=args.index_jsonl,
        probe_json=args.probe_json,
        summary_json=args.summary_json,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(report, args.markdown_out)
    print(json.dumps({"out": str(args.out), "markdown_out": str(args.markdown_out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
