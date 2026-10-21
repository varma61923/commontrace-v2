from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

_PYTEST_PASS = 0
_PYTEST_FAIL = 1


def from_test_exit_code(returncode: int) -> bool | None:
    """A test runner's process exit code, read as pytest's own convention."""
    if returncode == _PYTEST_PASS:
        return True
    if returncode == _PYTEST_FAIL:
        return False
    return None


def from_retry_count(retries: int, *, max_acceptable: int) -> bool:
    """Succeeded if the task needed at most `max_acceptable` retries."""
    if retries < 0:
        raise ValueError(f"retries must be >= 0, got {retries}")
    if max_acceptable < 0:
        raise ValueError(f"max_acceptable must be >= 0, got {max_acceptable}")
    return retries <= max_acceptable


def from_ticket_transition(
    new_status: str,
    *,
    resolved_statuses: frozenset[str] | set[str],
    reopened_statuses: frozenset[str] | set[str],
) -> bool | None:
    if new_status in resolved_statuses:
        return True
    if new_status in reopened_statuses:
        return False
    return None


def from_csat(score: float, *, scale_max: float, threshold_fraction: float = 0.6) -> bool:
    if scale_max <= 0:
        raise ValueError(f"scale_max must be > 0, got {scale_max}")
    if not 0.0 < threshold_fraction < 1.0:
        raise ValueError(f"threshold_fraction must be in (0, 1), got {threshold_fraction}")
    return (score / scale_max) >= threshold_fraction


def from_safety_stop(stopped: bool) -> bool:
    return not stopped


def from_human_takeover(human_took_over: bool) -> bool:
    return not human_took_over


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _check_window(window_days: float) -> timedelta:
    if not window_days > 0 or not math.isfinite(window_days):
        raise ValueError(f"window_days must be a positive number, got {window_days}")
    return timedelta(days=window_days)


def from_event_within_window(
    event_at: datetime | None,
    *,
    started_at: datetime,
    window_days: float,
    now: datetime | None = None,
) -> bool | None:
    window = _check_window(window_days)
    started = _aware(started_at)
    if event_at is not None:
        event = _aware(event_at)
        if event < started:
            return None
        return event - started <= window
    current = _aware(now) if now is not None else datetime.now(timezone.utc)
    return False if current - started > window else None


def from_no_reversal(
    reversal_at: datetime | None,
    *,
    started_at: datetime,
    window_days: float,
    now: datetime | None = None,
) -> bool | None:
    window = _check_window(window_days)
    started = _aware(started_at)
    if reversal_at is not None:
        reversal = _aware(reversal_at)
        if reversal < started:
            return None
        if reversal - started <= window:
            return False
        return True
    current = _aware(now) if now is not None else datetime.now(timezone.utc)
    return True if current - started > window else None


def from_threshold(
    value: float | None, *, minimum: float | None = None, maximum: float | None = None,
) -> bool | None:
    if minimum is None and maximum is None:
        raise ValueError("give a minimum, a maximum, or both")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"minimum {minimum} is above maximum {maximum}")
    if value is None or not math.isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def from_all(*signals: bool | None) -> bool | None:
    if not signals:
        raise ValueError("from_all needs at least one signal")
    if any(s is False for s in signals):
        return False
    return None if any(s is None for s in signals) else True


def from_any(*signals: bool | None) -> bool | None:
    if not signals:
        raise ValueError("from_any needs at least one signal")
    if any(s is True for s in signals):
        return True
    return None if any(s is None for s in signals) else False
