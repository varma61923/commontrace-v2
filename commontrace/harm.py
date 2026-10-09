"""What retrieval does with a lesson the experiment has shown makes outcomes worse."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from commontrace import experiment

POLICY_INFORM = "inform"
POLICY_WITHDRAW = "withdraw"
POLICIES = (POLICY_INFORM, POLICY_WITHDRAW)

REASON = "measured_harm"


def hurts(by_id: dict[str, dict]) -> dict[str, dict]:
    """The entries of an evidence map whose verdict is HURTS."""
    return {
        key: ev for key, ev in by_id.items()
        if ev.get("verdict") == experiment.VERDICT_HURTS
    }

def helps(by_id: dict[str, dict]) -> dict[str, dict]:
    """The entries of an evidence map whose verdict is HELPS (graduation candidates)."""
    return {
        key: ev for key, ev in by_id.items()
        if ev.get("verdict") == experiment.VERDICT_HELPS
    }


T = TypeVar("T")


def split(
    ranked: Iterable[T],
    withdrawn_slugs: dict[str, dict] | set[str],
    core_slugs: set[str],
    top_k: int,
    slug_of=lambda item: item.slug,
) -> tuple[list[T], list[T]]:
    """(kept, withdrawn) from a ranking made with `top_k + len(withdrawn)`."""
    kept: list[T] = []
    removed: list[T] = []
    for position, item in enumerate(ranked):
        slug = slug_of(item)
        if slug in withdrawn_slugs and slug not in core_slugs:
            if position < top_k:
                removed.append(item)
        elif len(kept) < top_k:
            kept.append(item)
    return kept, removed


def note(n: int) -> str:
    """Return the human-readable explanation for measured-harm withdrawals."""
    return (
        f"{n} matching lesson(s) were not injected because this store's experiment "
        "measured them making outcomes WORSE (verdict HURTS on an anytime-valid "
        "boundary) and the store withdraws such lessons. Their evidence is attached. "
        "An operator can rewrite one and start a fresh randomization to re-test it, "
        "or turn this off with `commontrace retrieval --on-harm inform`."
    )
