"""Automatic outcome detection: turn a signal an application ALREADY has --
a test runner's exit code, a ticket's status transition, a CSAT score, a
retry count, whether a human had to step in -- into the plain `succeeded:
bool` `holdout_io.record_outcome`/`CausalMemory.record_outcome` need,
without a caller hand-writing that mapping (and its edge cases) themselves
at every call site.

WHY EACH FUNCTION RETURNS `bool | None`, NOT JUST `bool`
----------------------------------------------------------
A signal is sometimes genuinely ambiguous -- a test run that collected zero
tests, a ticket status this store's caller didn't list as either resolved
or reopened. Guessing `True` or `False` for that case would silently
mislabel an occasion, and a mislabelled occasion is not neutral: it moves a
real observation into the wrong arm of a randomized comparison. `None`
means "this signal does not answer the question"; callers that receive it
should not report an outcome for that occasion at all (`record_outcome` is
still optional per occasion -- an unreported occasion is missing data,
which the experiment already accounts for; a wrongly reported one is not).

TIME WINDOWS
------------
Most business outcomes are not known when the work ends: a ticket is only
"resolved" if it stays resolved for a week, an outreach only "worked" if a
reply arrives within two. `from_event_within_window` and `from_no_reversal`
answer `None` while the window is still open, so an occasion is never labelled
from an outcome that has not had time to happen.

WHAT THIS MODULE DOES NOT DO
-------------------------------
Choose how several signals combine. `from_all` and `from_any` are the two
explicit three-valued rules (a caller names which one its function means);
anything subtler is the caller's. It also does not claim a precision figure
against a labelled sample. Both need a real fleet's data
to do honestly -- a stated combination rule invented without one would be
exactly the kind of unverifiable number this product refuses to publish
elsewhere (see STRATEGY.md's own discipline about §11.1's 10.9%). A caller
with more than one signal available combines them according to what THEIR
fleet's signals actually mean; this module gives each signal cleanly, not a
merged opinion.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

#: pytest's own exit codes (https://docs.pytest.org exit-code reference):
#: 0 = all collected tests passed, 1 = some failed. 2 (interrupted by the
#: user/a signal), 3 (internal error), 4 (usage error) and 5 (no tests were
#: collected at all) all say something OTHER than pass/fail, and are
#: deliberately NOT mapped to False -- an internal pytest crash is not
#: evidence the memory being measured made the task fail.
_PYTEST_PASS = 0
_PYTEST_FAIL = 1


def from_test_exit_code(returncode: int) -> bool | None:
    """A test runner's process exit code, read as pytest's own convention.

    Any non-pytest test runner that also uses "0 = pass, 1 = some tests
    failed" (most do -- it is the same convention `commontrace/cli.py`'s
    own `main()` uses for a clean run) is covered by the same mapping.
    """
    if returncode == _PYTEST_PASS:
        return True
    if returncode == _PYTEST_FAIL:
        return False
    return None


def from_retry_count(retries: int, *, max_acceptable: int) -> bool:
    """Succeeded if the task needed at most `max_acceptable` retries.

    Unlike the other detectors here, this one is never ambiguous: a retry
    count is always a plain integer with a caller-chosen threshold, so
    there is no reading of it that means "unknown" the way a stray exit
    code or an unlisted ticket status does.
    """
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
    """A ticketing system's status after this occasion, read against
    caller-supplied vocabularies rather than a hardcoded vendor's status
    names -- "resolved"/"closed" (Zendesk), "Done"/"Resolved" (Jira), and
    everything else in between are the caller's own taxonomy to name, not
    this module's to guess at.

    A status in neither set answers nothing (`None`) rather than being
    read as a default -- an in-progress/pending status is not evidence the
    memory helped OR hurt; it means the occasion has not concluded yet.
    """
    if new_status in resolved_statuses:
        return True
    if new_status in reopened_statuses:
        return False
    return None


def from_csat(score: float, *, scale_max: float, threshold_fraction: float = 0.6) -> bool:
    """A CSAT/NPS-shaped numeric rating, thresholded as a fraction of the
    scale's own maximum (default 60%) rather than a hardcoded absolute
    number -- a 1-5 scale and a 1-100 scale are the same rating expressed
    at different resolutions, and a fixed threshold would silently mean
    something different on each.
    """
    if scale_max <= 0:
        raise ValueError(f"scale_max must be > 0, got {scale_max}")
    if not 0.0 < threshold_fraction < 1.0:
        raise ValueError(f"threshold_fraction must be in (0, 1), got {threshold_fraction}")
    return (score / scale_max) >= threshold_fraction


def from_human_takeover(human_took_over: bool) -> bool:
    """A human stepping in to finish or correct the task is read as the
    task NOT having been resolved autonomously -- named as its own function
    (rather than a caller writing `not human_took_over` inline) so the
    convention is stated once, in the place `--draft`/reliability's own
    evidence-grounded reasoning already lives, rather than re-derived per
    caller and inevitably inverted by one of them eventually.
    """
    return not human_took_over


def _aware(moment: datetime) -> datetime:
    """Naive datetimes are read as UTC, so a naive and an aware value compare."""
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
    """Did a wanted event (a reply, a booked meeting, an accepted offer, a
    stage advance) happen within `window_days` of the occasion?

    True if it did. False if the window has closed without it, or it came
    after the window. `None` while the window is open with no event yet --
    the occasion has not concluded -- and for an event dated before the
    occasion started, which is a data error, not an answer.
    """
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
    """Did the result STAY done for `window_days` -- no reopen, no revert, no
    rollback?

    False if it was reversed inside the window. True once the window has
    closed with no reversal (a reversal after the window does not count
    against it). `None` while the window is open and nothing has reversed
    yet: success is only known at the end of the window.
    """
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
    """A measured quantity read against a bound: conversion rate at least X,
    position error at most Y, handle time within budget.

    `None` for a missing or non-finite measurement. At least one bound is
    required; with both, the value must sit inside them.
    """
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
    """Success only if every signal says so. Any False is a failure however
    the others read; otherwise any `None` leaves it undecided."""
    if not signals:
        raise ValueError("from_all needs at least one signal")
    if any(s is False for s in signals):
        return False
    return None if any(s is None for s in signals) else True


def from_any(*signals: bool | None) -> bool | None:
    """Success if at least one signal says so. Any True is a success; otherwise
    any `None` leaves it undecided, since a signal still open may yet say yes."""
    if not signals:
        raise ValueError("from_any needs at least one signal")
    if any(s is True for s in signals):
        return True
    return None if any(s is None for s in signals) else False
