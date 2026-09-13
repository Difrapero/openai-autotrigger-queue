from __future__ import annotations

import unittest
from datetime import datetime, timezone
from email.utils import format_datetime

from autotrigger.cooldown import detect_cooldown


class FakeResponse:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class FakeError(Exception):
    def __init__(self, headers: dict[str, str] | None = None, body: object = None) -> None:
        super().__init__("rate limited")
        self.response = FakeResponse(headers or {})
        self.body = body


class CooldownTests(unittest.TestCase):
    def test_retry_after_decimal_seconds_has_priority(self) -> None:
        cooldown = detect_cooldown(
            FakeError({"Retry-After": "14250.125", "x-ratelimit-reset-requests": "1s"}),
            now=1_700_000_000,
        )
        self.assertIsNotNone(cooldown)
        self.assertEqual(cooldown.seconds, 14250.125)
        self.assertEqual(cooldown.source, "header:retry-after")

    def test_retry_after_http_date(self) -> None:
        now = 1_700_000_000.0
        date = format_datetime(datetime.fromtimestamp(now + 90, timezone.utc), usegmt=True)
        cooldown = detect_cooldown(FakeError({"Retry-After": date}), now=now)
        self.assertEqual(cooldown.seconds, 90.0)

    def test_longest_reset_header_wins(self) -> None:
        cooldown = detect_cooldown(
            FakeError(
                {
                    "x-ratelimit-reset-requests": "1m2.5s",
                    "x-ratelimit-reset-tokens": "250ms",
                }
            ),
            now=1_700_000_000,
        )
        self.assertEqual(cooldown.seconds, 62.5)
        self.assertEqual(cooldown.source, "header:x-ratelimit-reset-requests")

    def test_structured_body_fallback(self) -> None:
        cooldown = detect_cooldown(FakeError(body={"error": {"retry_after_ms": 2500}}))
        self.assertEqual(cooldown.seconds, 2.5)

    def test_message_fallback(self) -> None:
        error = FakeError(body={"error": {"message": "Please try again in 1m30.5s."}})
        cooldown = detect_cooldown(error)
        self.assertEqual(cooldown.seconds, 90.5)

    def test_direct_sdk_attribute_fallback(self) -> None:
        error = FakeError()
        error.retry_after_ms = 14250
        cooldown = detect_cooldown(error)
        self.assertEqual(cooldown.seconds, 14.25)
        self.assertEqual(cooldown.source, "error-attribute:retry_after_ms")

    def test_message_milliseconds_keep_their_scale(self) -> None:
        error = FakeError(body={"error": {"message": "Please retry after 250 milliseconds"}})
        cooldown = detect_cooldown(error)
        self.assertEqual(cooldown.seconds, 0.25)

    def test_missing_clock_does_not_invent_delay(self) -> None:
        self.assertIsNone(detect_cooldown(FakeError(body={"error": {"code": "insufficient_quota"}})))


if __name__ == "__main__":
    unittest.main()
