"""Extract an exact retry delay from an OpenAI HTTP 429 exception.

No guessed/exponential delay is used here.  A retry is scheduled only when the
server supplies a usable value in HTTP headers, structured error data, or its
human-readable error message.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping


_DURATION_TOKEN = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|us|µs|d|h|m|s)", re.IGNORECASE
)
_MESSAGE_DELAY = re.compile(
    r"(?:try\s+again\s+in|retry\s+(?:again\s+)?(?:in|after)|wait(?:\s+for)?)\s+"
    r"(?P<delay>(?:\d+(?:\.\d+)?\s*"
    r"(?:milliseconds?|msecs?|ms|microseconds?|us|µs|days?|d|hours?|hrs?|h|"
    r"minutes?|mins?|m|seconds?|secs?|s)\s*)+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Cooldown:
    """An API-provided cooldown and the evidence used to derive it."""

    seconds: float
    source: str
    raw_value: str

    def resume_at(self, now: float | None = None) -> float:
        return (time.time() if now is None else now) + self.seconds


def _headers_from_exception(error: BaseException) -> dict[str, str]:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(error, "headers", None)
    if not headers:
        return {}
    try:
        return {str(key).lower(): str(value).strip() for key, value in headers.items()}
    except (AttributeError, TypeError):
        return {}


def _parse_http_date(value: str, now: float) -> float | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, parsed.timestamp() - now)


def _parse_duration(value: str) -> float | None:
    """Parse OpenAI reset durations such as ``1h2m3.5s`` or ``250ms``."""

    text = value.strip().lower()
    if not text:
        return None

    # retry-after commonly contains a plain number of seconds.
    try:
        return max(0.0, float(text))
    except ValueError:
        pass

    normalized = text
    # Replace long units before short ones ("milliseconds" ends in
    # "seconds") so their scale is not accidentally changed.
    for pattern, replacement in (
        (r"\bmilliseconds?\b|\bmsecs?\b", "ms"),
        (r"\bmicroseconds?\b", "us"),
        (r"\bdays?\b", "d"),
        (r"\bhours?\b|\bhrs?\b", "h"),
        (r"\bminutes?\b|\bmins?\b", "m"),
        (r"\bseconds?\b|\bsecs?\b", "s"),
    ):
        normalized = re.sub(pattern, replacement, normalized)
    multipliers = {
        "d": 86_400.0,
        "h": 3_600.0,
        "m": 60.0,
        "s": 1.0,
        "ms": 0.001,
        "us": 0.000_001,
        "µs": 0.000_001,
    }
    matches = list(_DURATION_TOKEN.finditer(normalized))
    if not matches:
        return None
    leftover = _DURATION_TOKEN.sub("", normalized)
    if leftover.strip(" ,"):
        return None
    return sum(float(match["value"]) * multipliers[match["unit"].lower()] for match in matches)


def _parse_reset_value(value: str, now: float) -> float | None:
    """Parse a duration, Unix timestamp, ISO timestamp, or HTTP date."""

    text = value.strip()
    try:
        numeric = float(text)
        # Values this large are timestamps rather than plausible wait durations.
        if numeric > 100_000_000:
            if numeric > 100_000_000_000:  # milliseconds since epoch
                numeric /= 1_000.0
            return max(0.0, numeric - now)
        return max(0.0, numeric)
    except ValueError:
        pass

    duration = _parse_duration(text)
    if duration is not None:
        return duration
    http_delay = _parse_http_date(text, now)
    if http_delay is not None:
        return http_delay
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, parsed.timestamp() - now)
    except (ValueError, OverflowError):
        return None


def _find_mapping_value(data: Any, wanted_keys: set[str]) -> tuple[str, Any] | None:
    if isinstance(data, Mapping):
        for key, value in data.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in wanted_keys and value is not None:
                return normalized, value
        for value in data.values():
            found = _find_mapping_value(value, wanted_keys)
            if found:
                return found
    elif isinstance(data, (list, tuple)):
        for value in data:
            found = _find_mapping_value(value, wanted_keys)
            if found:
                return found
    return None


def _message_from_error(error: BaseException, body: Any) -> str:
    found = _find_mapping_value(body, {"message", "detail"})
    if found:
        return str(found[1])
    return str(getattr(error, "message", "") or error)


def detect_cooldown(error: BaseException, now: float | None = None) -> Cooldown | None:
    """Return the exact server-provided cooldown found on an SDK exception.

    Precedence follows HTTP semantics: ``Retry-After`` is authoritative.  If it
    is absent, all applicable rate-limit reset headers are inspected and the
    longest delay wins, preventing an early retry when both request and token
    limits are exhausted.  Structured SDK error fields and the error message
    are used as final fallbacks.
    """

    current_time = time.time() if now is None else now
    headers = _headers_from_exception(error)

    retry_after = headers.get("retry-after")
    if retry_after:
        seconds = _parse_duration(retry_after)
        if seconds is None:
            seconds = _parse_http_date(retry_after, current_time)
        if seconds is not None and seconds > 0:
            return Cooldown(seconds, "header:retry-after", retry_after)

    retry_after_ms = headers.get("retry-after-ms")
    if retry_after_ms:
        try:
            seconds = max(0.0, float(retry_after_ms) / 1_000.0)
        except ValueError:
            seconds = 0.0
        if seconds > 0:
            return Cooldown(seconds, "header:retry-after-ms", retry_after_ms)

    reset_candidates: list[Cooldown] = []
    for key, raw_value in headers.items():
        if key.startswith("x-ratelimit-reset-"):
            seconds = _parse_reset_value(raw_value, current_time)
            if seconds is not None and seconds > 0:
                reset_candidates.append(Cooldown(seconds, f"header:{key}", raw_value))
    if reset_candidates:
        return max(reset_candidates, key=lambda item: item.seconds)

    # Some SDKs or wrappers expose the server value as a direct exception
    # attribute rather than preserving a structured body.
    for attribute, divisor, is_reset in (
        ("retry_after_ms", 1_000.0, False),
        ("retry_after_seconds", 1.0, False),
        ("retry_after", 1.0, False),
        ("reset_at", 1.0, True),
    ):
        raw = getattr(error, attribute, None)
        if raw is None:
            continue
        seconds = (
            _parse_reset_value(str(raw), current_time)
            if is_reset
            else _parse_duration(str(raw))
        )
        if seconds is not None:
            seconds /= divisor
        if seconds is not None and seconds > 0:
            return Cooldown(seconds, f"error-attribute:{attribute}", str(raw))

    body = getattr(error, "body", None)
    for keys, divisor in (
        ({"retry_after_ms", "retryafterms"}, 1_000.0),
        ({"retry_after", "retryafter", "retry_after_seconds"}, 1.0),
        ({"reset_at", "resetat"}, 1.0),
    ):
        found = _find_mapping_value(body, keys)
        if not found:
            continue
        field, raw = found
        if field in {"reset_at", "resetat"}:
            seconds = _parse_reset_value(str(raw), current_time)
        else:
            seconds = _parse_duration(str(raw))
            if seconds is not None:
                seconds /= divisor
        if seconds is not None and seconds > 0:
            return Cooldown(seconds, f"error:{field}", str(raw))

    message = _message_from_error(error, body)
    match = _MESSAGE_DELAY.search(message)
    if match:
        seconds = _parse_duration(match["delay"])
        if seconds is not None and seconds > 0:
            return Cooldown(seconds, "error:message", match["delay"])
    return None
