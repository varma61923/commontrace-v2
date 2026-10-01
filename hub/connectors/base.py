"""What every outcome connector shares.

A connector turns what a system of record says happened (a ticket solved, a
pull request merged) into the one thing the causal measurement needs: whether
an occasion went well. Everything vendor-specific lives in a provider module;
this file holds the parts that must be identical for all of them, because each
is a place a connector could be wrong in a way that corrupts the measurement or
opens a hole.

SIGNALS, NOT OUTCOMES
---------------------
A provider emits `Signal`s. It does not decide the outcome, because most
outcomes are not final when the event arrives: a ticket marked solved only
counts if it stays solved. So:

  CANDIDATE   looks like success; becomes success once the window passes with no
              REVERSAL (window 0 means immediately)
  SUCCESS     final success
  FAILURE     final failure
  REVERSAL    undoes a CANDIDATE (reopened, reverted); a failure if one is pending

An occasion is never labelled from an outcome that has not had time to happen,
which is the same rule `commontrace.outcome_detect` applies on the client.

AUTHENTICITY
------------
Each provider verifies the vendor's documented signature over the RAW body,
comparing in constant time. Where the vendor signs a timestamp, a delivery
outside TOLERANCE_SECONDS is refused. Every delivery, signed timestamp or not,
also needs a `delivery_id` (the vendor's unique event or delivery id) that the
ledger dedupes on; a delivery without one is refused rather than accepted with
no replay protection.
"""
from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

#: How far a signed timestamp may sit from now. Matches hub/billing.py.
TOLERANCE_SECONDS = 300

CANDIDATE = "candidate_success"
SUCCESS = "success"
FAILURE = "failure"
REVERSAL = "reversal"
KINDS = (CANDIDATE, SUCCESS, FAILURE, REVERSAL)

#: HoldoutObservation.occasion_id is String(128); a longer id could never match one.
MAX_ID_CHARS = 128


class SignatureError(Exception):
    """The delivery is not authentic, is stale, or cannot be replay-protected.

    `status` is what the route answers; the message is for logs, never for the caller."""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


class ConfigError(ValueError):
    """A connector config that cannot be used."""


@dataclass(frozen=True)
class Signal:
    kind: str
    #: What the agent used as `occasion_id`. Empty for a REVERSAL found by `ref`.
    occasion_id: str
    at: datetime
    #: The vendor's id for the event that produced this signal.
    event_id: str
    #: A vendor reference that links a later reversal back to this occasion
    #: (a merge commit sha). Stored on a CANDIDATE, matched by a REVERSAL.
    ref: str = ""
    note: str = ""


def header(headers: Mapping[str, str], name: str) -> str:
    """A header value, case-insensitively, or ''. Starlette's Headers already
    is; a plain dict (tests, other callers) is not."""
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
    """An occasion id from a vendor payload: a non-empty string under the
    storage limit. Numbers are accepted (vendors send ids either way)."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{label} is missing or not an id")
    text = str(value).strip()
    if not text or len(text) > MAX_ID_CHARS:
        raise ValueError(f"{label} is empty or longer than {MAX_ID_CHARS} characters")
    return text


_FRACTION = re.compile(r"(?<=:\d\d)\.(\d+)")


def parse_timestamp(value: str) -> datetime:
    """ISO-8601 as vendors send it. Before 3.11 `fromisoformat` rejects a trailing Z and any fraction that is
    not exactly 3 or 6 digits, and Zendesk sends nanoseconds; the fraction is cut (never rounded) to 6."""
    from datetime import timezone

    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def common_config(config: object, *, allowed: set[str]) -> dict:
    """The options every provider shares, validated: `occasion_prefix` and
    `window_days`, plus the provider's own `allowed` keys."""
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
