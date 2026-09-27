"""What retrieval does with a lesson the experiment has shown makes outcomes worse.

Until now, a HURTS verdict was reported and nothing else happened: the
lesson kept being retrieved, ranked and injected exactly as before, so a
store could hold the measured proof that one of its lessons was costing it
outcomes and keep handing that lesson to every agent that asked. The
verdict reached the agent (commontrace/evidence.py) and relied on the agent
to act on it.

A store that opts in (`commontrace retrieval --on-harm withdraw`) stops
injecting such a lesson. It is still named on every retrieval it matched,
with its evidence, so nothing disappears silently -- an agent is told the
lesson exists and why it was not handed over, and an operator can find it.

WHY THIS DOES NOT CORRUPT THE EXPERIMENT THAT PRODUCED THE VERDICT.

  * The decision is made BEFORE arms are assigned, like the budget
    (commontrace/dosage.py): a withdrawn lesson is never eligible, so it is
    never logged as treated or withheld on an occasion it was absent from.
  * Every other lesson's comparison is untouched. Withdrawal changes the
    background both arms of every other lesson share, at the same moment
    for both, which is what randomization is robust to.
  * The withdrawn lesson's own estimate stops accruing where it stood.
    Stopping because a boundary was crossed is exactly the look that
    inflates a fixed-threshold test, so the verdict acted on here comes
    from the anytime-valid analysis (experiment.analyze(sequential=True)),
    whose guarantee holds at every sample size including the one it
    stopped at. Clinical trials stop for harm on the same reasoning.
  * Ranking happens with the lesson still in the corpus and it is removed
    afterwards, so the relevance every other lesson scores -- and therefore
    which of them clear the floor -- is identical with and without this.

A verdict is only acted on while the experiment that produced it is
readable and current: a COMPROMISED audit yields no evidence, and a new
randomization (a changed salt) starts every lesson from no verdict at all,
which is also how a withdrawn lesson gets a second trial after it is
rewritten -- the rewrite changes the treatment, and measuring the new text
needs a fresh randomization anyway (integrity.check_treatment_stability).

`core: true` lessons are exempt. They are never randomized, so they cannot
earn a verdict under their current flag, and core is the operator's
explicit statement that the lesson is present every time.

Off by default ("inform"), because switching it on changes what a running
fleet is given, and that is a decision, not an upgrade side effect.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

# Deliberately pure: the Hub imports this module (hub/crud.py, hub/manage.py)
# into an image that carries no PyYAML and no local-store code, so reading
# the local store's evidence lives in commontrace/evidence.py:withdrawn.
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

T = TypeVar("T")


def split(
    ranked: Iterable[T],
    withdrawn_slugs: dict[str, dict] | set[str],
    core_slugs: set[str],
    top_k: int,
    slug_of=lambda item: item.slug,
) -> tuple[list[T], list[T]]:
    """(kept, withdrawn) from a ranking made with `top_k + len(withdrawn)`.

    The caller over-fetches by the number of withdrawn lessons so that
    removing them backfills from the next-ranked lesson rather than
    returning fewer than `top_k` -- a withdrawn lesson leaves the slot to
    the lesson that would have been there had it never existed.

    `withdrawn` holds only the lessons that would have been in the top
    `top_k` without the policy: one ranked below the cut would not have
    been handed over anyway, and naming it would say this retrieval kept
    out something it was never going to give.
    """
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
    return (
        f"{n} matching lesson(s) were not injected because this store's experiment "
        "measured them making outcomes WORSE (verdict HURTS on an anytime-valid "
        "boundary) and the store withdraws such lessons. Their evidence is attached. "
        "An operator can rewrite one and start a fresh randomization to re-test it, "
        "or turn this off with `commontrace retrieval --on-harm inform`."
    )
