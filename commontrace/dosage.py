"""How much memory to inject, and which memory is always injected.

WHY THIS EXISTS
---------------
Retrieval answered "which lessons match this task, best first" and stopped
there. Two things it did not answer, both of which decide what an agent
actually receives:

**How much.** `top_k` bounds the COUNT and says nothing about the size. Ten
terse lessons and ten pages of prose are the same `top_k=10`, and the second
one displaces the task itself out of the context window. This product's own
measurements put one retrieval page at roughly 1,160 tokens; a store whose
lessons grew would silently start costing several times that, on every
occasion, forever, with nothing reporting the change.

**Which is unconditional.** Some rules are not "relevant to this task" --
they are how the fleet operates. "Never retry a payment without an
idempotency key" does not want to be competing for a top-10 slot against
whatever happens to share vocabulary with the current request; it wants to
be there every time. Without that distinction, the only way to make a rule
reliable was to make it match everything, which is the same thing as making
retrieval worse.

So: lessons may be marked `core: true`, which admits them ahead of the
matched set, and everything is admitted against a BUDGET in characters as
well as count. Nothing is silently truncated -- what did not fit is returned
with the reason, because an agent that was given nine of ten lessons and
told it was given ten will act on the missing one's absence as though it
were the fleet's position.

WHY CORE IS STILL BUDGETED
--------------------------
A core lesson is admitted first, not admitted unconditionally. A fleet that
marks forty lessons core has not thereby earned forty lessons' worth of
context, and the failure mode of the unconditional reading is the worst one
available: the always-on set crowds out every matched lesson, so retrieval
appears to stop working, with no error. Core lessons compete only with each
other, by importance, and the overflow is reported loudly enough to be
fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Frontmatter flag marking a lesson as always-on.
CORE_FIELD = "core"

#: What a caller gets if it expresses no opinion. The count matches
#: retrieval's historical `top_k`; the character budget is set so a full
#: page of lessons stays near the ~1,160-token cost this product already
#: measured for one retrieval, rather than silently growing with the store.
DEFAULT_MAX_LESSONS = 10
DEFAULT_MAX_CHARS = 8_000

REASON_COUNT = "count budget reached"
REASON_CHARS = "character budget reached"


@dataclass(frozen=True)
class Budget:
    max_lessons: int = DEFAULT_MAX_LESSONS
    max_chars: int = DEFAULT_MAX_CHARS

    def __post_init__(self) -> None:
        if self.max_lessons < 0 or self.max_chars < 0:
            raise ValueError(
                "a budget cannot be negative; use 0 to inject nothing "
                f"(got max_lessons={self.max_lessons}, max_chars={self.max_chars})"
            )


@dataclass(frozen=True)
class Candidate:
    """One lesson considered for injection, with the size it would cost."""

    slug: str
    text: str
    #: True for a lesson the store marks as always-on.
    core: bool = False
    #: Ranking position from retrieval, for the matched set. Ignored for core.
    relevance: float = 0.0
    importance: int = 0
    revision: str = ""

    @property
    def cost(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class Dropped:
    slug: str
    reason: str
    core: bool = False


@dataclass(frozen=True)
class Dose:
    """What an agent will actually be given, and what it will not."""

    admitted: tuple[Candidate, ...] = field(default_factory=tuple)
    dropped: tuple[Dropped, ...] = field(default_factory=tuple)
    chars_used: int = 0
    budget: Budget = field(default_factory=Budget)

    @property
    def core_admitted(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.admitted if c.core)

    @property
    def core_dropped(self) -> tuple[Dropped, ...]:
        """Always-on lessons that did not fit. Separated because this is not
        an ordinary budget outcome -- it means the fleet's unconditional
        rules do not fit in the space allowed for them, which is a
        configuration error rather than a ranking one."""
        return tuple(d for d in self.dropped if d.core)

    @property
    def chars_available(self) -> int:
        return max(0, self.budget.max_chars - self.chars_used)

    def gauge(self) -> str:
        """One line an operator or an agent can read to see the cost."""
        pct = (
            (self.chars_used / self.budget.max_chars * 100)
            if self.budget.max_chars else 0.0
        )
        line = (
            f"{len(self.admitted)}/{self.budget.max_lessons} lessons, "
            f"{self.chars_used:,}/{self.budget.max_chars:,} chars ({pct:.0f}%)"
        )
        if self.dropped:
            line += f", {len(self.dropped)} not injected"
        return line


def select(candidates, budget: Budget | None = None) -> Dose:
    """Admit as much as the budget allows: core first, then by rank.

    ORDER, and why it is this one:

      1. Core lessons, by importance then slug. They are the fleet's
         position and should not lose a slot to whatever happened to match
         today's vocabulary.
      2. Matched lessons, in the order retrieval returned them. Retrieval
         has already done the ranking; re-sorting here would quietly apply a
         second, different opinion about relevance.

    A candidate that does not fit the CHARACTER budget is skipped, and the
    next one is still considered -- a single oversized lesson should not
    silently truncate everything ranked below it. A candidate that does not
    fit the COUNT budget ends admission, since every subsequent one fails
    the same test.
    """
    budget = budget or Budget()
    items = list(candidates)

    core = sorted(
        (c for c in items if c.core),
        key=lambda c: (-int(c.importance or 0), c.slug),
    )
    matched = [c for c in items if not c.core]

    admitted: list[Candidate] = []
    dropped: list[Dropped] = []
    used = 0

    for group in (core, matched):
        for candidate in group:
            if len(admitted) >= budget.max_lessons:
                dropped.append(Dropped(candidate.slug, REASON_COUNT, candidate.core))
                continue
            if used + candidate.cost > budget.max_chars:
                dropped.append(Dropped(candidate.slug, REASON_CHARS, candidate.core))
                continue
            admitted.append(candidate)
            used += candidate.cost

    return Dose(
        admitted=tuple(admitted),
        dropped=tuple(dropped),
        chars_used=used,
        budget=budget,
    )


def is_core(fm: dict) -> bool:
    """Whether a lesson's frontmatter marks it always-on.

    Tolerant of the shapes a hand-edited YAML file produces ("true", "yes",
    True) and of the field being absent, which is the overwhelmingly common
    case: a store that has never heard of core lessons has none.
    """
    raw = fm.get(CORE_FIELD, False)
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "yes", "1", "on")
    return bool(raw)
