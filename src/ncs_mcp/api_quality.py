from __future__ import annotations

import re
from typing import Any


API_ELEMENT_UNMATCHED_ISSUE_TYPE = "api_element_unmatched"
API_ELEMENT_COLLECTION_FAILURE_ISSUE_TYPE = "api_element_collection_failure"
API_ELEMENT_FAILURE_ISSUE_TYPES = (
    API_ELEMENT_UNMATCHED_ISSUE_TYPE,
    API_ELEMENT_COLLECTION_FAILURE_ISSUE_TYPE,
)

_API_ELEMENT_FAILURE_DETAIL_PATTERNS = (
    re.compile(r"^NCS006 request failed after retries(?:[:.].*)?$", re.IGNORECASE),
    re.compile(r"^NCS006 returned resultCode=\d+:", re.IGNORECASE),
)


def is_api_element_collection_failure_issue(
    issue_type: str | None,
    *,
    issue_detail: str | None = None,
    api_match_status: str | None = None,
) -> bool:
    normalized_issue_type = str(issue_type or "").strip()
    if normalized_issue_type == API_ELEMENT_COLLECTION_FAILURE_ISSUE_TYPE:
        return True
    if normalized_issue_type != API_ELEMENT_UNMATCHED_ISSUE_TYPE:
        return False
    if str(api_match_status or "").strip() == "api_failed":
        return True
    detail = str(issue_detail or "").strip()
    return any(pattern.match(detail) for pattern in _API_ELEMENT_FAILURE_DETAIL_PATTERNS)


def normalize_api_element_issue_type(
    issue_type: str | None,
    *,
    issue_detail: str | None = None,
    api_match_status: str | None = None,
) -> str:
    normalized_issue_type = str(issue_type or "").strip()
    if is_api_element_collection_failure_issue(
        normalized_issue_type,
        issue_detail=issue_detail,
        api_match_status=api_match_status,
    ):
        return API_ELEMENT_COLLECTION_FAILURE_ISSUE_TYPE
    if normalized_issue_type == API_ELEMENT_UNMATCHED_ISSUE_TYPE:
        return API_ELEMENT_UNMATCHED_ISSUE_TYPE
    return normalized_issue_type


def normalize_api_element_issue(
    issue: dict[str, Any],
    *,
    api_match_status: str | None = None,
) -> dict[str, Any]:
    issue_type = str(issue.get("issue_type") or "")
    normalized_issue_type = normalize_api_element_issue_type(
        issue_type,
        issue_detail=str(issue.get("issue_detail") or ""),
        api_match_status=api_match_status,
    )
    if normalized_issue_type == issue_type:
        return issue
    normalized_issue = dict(issue)
    normalized_issue["source_issue_type"] = issue_type
    normalized_issue["issue_type"] = normalized_issue_type
    return normalized_issue
