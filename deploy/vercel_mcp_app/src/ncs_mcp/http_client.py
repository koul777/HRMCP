from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import requests


RETRYABLE_STATUS_CODES = {429, 502, 503, 504}


def retry_delay_seconds(
    *,
    attempt_index: int,
    retry_backoff_seconds: float,
    retry_after: str | None = None,
) -> float:
    if retry_after and retry_after.isdigit():
        return max(0.0, float(retry_after))
    return max(0.0, float(retry_backoff_seconds)) * max(1, attempt_index + 1)


def get_with_retries(
    url: str,
    *,
    params: dict[str, Any],
    timeout: int | float,
    max_retries: int = 2,
    retry_backoff_seconds: float = 1.0,
    retry_statuses: set[int] | None = None,
    request_get: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
):
    request = request_get or requests.get
    sleeper = sleep or time.sleep
    retryable_statuses = retry_statuses or RETRYABLE_STATUS_CODES
    attempts = max(1, int(max_retries) + 1)
    for attempt_index in range(attempts):
        try:
            response = request(url, params=params, timeout=timeout)
        except requests.RequestException:
            if attempt_index + 1 >= attempts:
                raise
            sleeper(
                retry_delay_seconds(
                    attempt_index=attempt_index,
                    retry_backoff_seconds=retry_backoff_seconds,
                )
            )
            continue
        if response.status_code in retryable_statuses and attempt_index + 1 < attempts:
            sleeper(
                retry_delay_seconds(
                    attempt_index=attempt_index,
                    retry_backoff_seconds=retry_backoff_seconds,
                    retry_after=response.headers.get("Retry-After"),
                )
            )
            continue
        return response
    raise RuntimeError("unreachable retry state")
