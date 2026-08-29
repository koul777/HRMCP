from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

TEXT_EXPECTATIONS = {
    "README.md": [
        "NCS 기반 HR 실무용 MCP",
        "교육훈련 계획 수립",
        "NCS 중심",
    ],
    "docs/NCS_MCP_USER_GUIDE_KO.md": ["NCS 훈련 추천 MCP 사용자 가이드", "경력개발", "직무 전환"],
    "docs/MCP_RELEASE_CHECKLIST.md": ["API keys", "Docker", "/ready"],
}

MOJIBAKE_MARKERS = [
    "\ufffd",
    "?덈젴",
    "援먯쑁",
    "吏곷Т",
    "寃쎌쁺",
]


def check_text_file(path: Path, required_terms: list[str]) -> list[str]:
    issues: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [f"{path}: not valid UTF-8 ({exc})"]
    for marker in MOJIBAKE_MARKERS:
        if marker in text:
            issues.append(f"{path}: contains mojibake marker {marker!r}")
    for term in required_terms:
        if term not in text:
            issues.append(f"{path}: missing expected text {term!r}")
    return issues


def check_contract(path: Path) -> list[str]:
    issues = check_text_file(path, [])
    if issues:
        return issues
    payload = json.loads(path.read_text(encoding="utf-8"))
    aliases_by_tool = {
        tool["name"]: tool.get("aliases", [])
        for tool in payload.get("tools", [])
        if isinstance(tool, dict)
    }
    if "훈련" not in aliases_by_tool.get("ncs_training", []):
        issues.append(f"{path}: ncs_training aliases do not include '훈련'")
    if payload.get("surface", {}).get("active_tool_count") != 7:
        issues.append(f"{path}: public active_tool_count is not 7")
    return issues


def main() -> int:
    issues: list[str] = []
    for relative, terms in TEXT_EXPECTATIONS.items():
        issues.extend(check_text_file(ROOT / relative, terms))
    issues.extend(check_contract(ROOT / "mcp" / "ncs-tool-contract.json"))
    result = {"ok": not issues, "issues": issues}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
