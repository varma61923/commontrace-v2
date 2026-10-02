"""What every outcome connector shares."""
from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

TOLERANCE_SECONDS = 300

CANDIDATE = "candidate_success"
SUCCESS = "success"
FAILURE = "failure"
REVERSAL = "reversal"
KINDS = (CANDIDATE, SUCCESS, FAILURE, REVERSAL)

MAX_ID_CHARS = 128


class SignatureError(Exception):
    """The delivery is not authentic, is stale, or cannot be replay-protected."""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


class ConfigError(ValueError):
    """A connector config that cannot be used."""


@dataclass(frozen=True)
class Signal:
    kind: str
    occasion_id: str
    at: datetime
    event_id: str
    ref: str = ""
    note: str = ""


def header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if value is None:
        lowered = name.lower()
        for key, candidate in headers.items():
            if key.lower() == lowered:
                return candidate
        return ""
    return value


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def clean_id(value: object, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{label} is missing or not an id")
    text = str(value).strip()
    if not text or len(text) > MAX_ID_CHARS:
        raise ValueError(f"{label} is empty or longer than {MAX_ID_CHARS} characters")
    return text


_FRACTION = re.compile(r"(?<=:\d\d)\.(\d+)")


def parse_timestamp(value: str) -> datetime:
    from datetime import timezone

    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def common_config(config: object, *, allowed: set[str]) -> dict:
    if not isinstance(config, dict):
        raise ConfigError("config must be an object")
    unknown = sorted(set(config) - allowed - {"occasion_prefix", "window_days"})
    if unknown:
        raise ConfigError(f"unknown config option(s): {', '.join(unknown)}")
    out = dict(config)
    prefix = out.get("occasion_prefix", "")
    if not isinstance(prefix, str) or len(prefix) > 64:
        raise ConfigError("occasion_prefix must be text of at most 64 characters")
    window = out.get("window_days", 0)
    if isinstance(window, bool) or not isinstance(window, (int, float)) or not 0 <= window <= 365:
        raise ConfigError("window_days must be a number from 0 to 365")
    out["occasion_prefix"], out["window_days"] = prefix, window
    return out
