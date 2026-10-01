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

WHAT THIS MODULE DOES NOT DO
-------------------------------
Reconcile multiple signals into one verdict when they disagree, or claim a
precision figure against a labelled sample. Both need a real fleet's data
to do honestly -- a stated combination rule invented without one would be
exactly the kind of unverifiable number this product refuses to publish
elsewhere (see STRATEGY.md's own discipline about §11.1's 10.9%). A caller
with more than one signal available combines them according to what THEIR
fleet's signals actually mean; this module gives each signal cleanly, not a
merged opinion.
"""
from __future__ import annotations

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
