from __future__ import annotations

import sys
import unittest
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.http_client import get_with_retries, retry_delay_seconds


class FakeResponse:
    def __init__(self, status_code: int, *, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = "<root />"


class HttpClientTests(unittest.TestCase):
    def test_retry_delay_uses_retry_after_when_numeric(self) -> None:
        self.assertEqual(
            retry_delay_seconds(attempt_index=0, retry_backoff_seconds=3, retry_after="7"),
            7.0,
        )
        self.assertEqual(
            retry_delay_seconds(attempt_index=1, retry_backoff_seconds=3, retry_after=None),
            6.0,
        )

    def test_get_with_retries_retries_retryable_status(self) -> None:
        calls: list[int] = []
        sleeps: list[float] = []
        responses = [FakeResponse(503), FakeResponse(200)]

        def fake_get(_url, *, params, timeout):
            calls.append(timeout)
            return responses.pop(0)

        response = get_with_retries(
            "https://example.test",
            params={"a": "b"},
            timeout=5,
            max_retries=2,
            retry_backoff_seconds=0.5,
            request_get=fake_get,
            sleep=sleeps.append,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [5, 5])
        self.assertEqual(sleeps, [0.5])

    def test_get_with_retries_retries_request_exceptions_then_raises(self) -> None:
        sleeps: list[float] = []

        def fake_get(_url, *, params, timeout):
            raise requests.Timeout("timeout")

        with self.assertRaises(requests.Timeout):
            get_with_retries(
                "https://example.test",
                params={},
                timeout=5,
                max_retries=1,
                retry_backoff_seconds=2,
                request_get=fake_get,
                sleep=sleeps.append,
            )

        self.assertEqual(sleeps, [2.0])


if __name__ == "__main__":
    unittest.main()
