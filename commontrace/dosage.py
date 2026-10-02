"""How much memory to inject, and which memory is always injected."""

from __future__ import annotations

from dataclasses import dataclass, field

CORE_FIELD = "core"

DEFAULT_MAX_LESSONS = 10
DEFAULT_MAX_CHARS = 8_000

REASON_COUNT = "count budget reached"
REASON_CHARS = "character budget reached"
REASON_REDUNDANT = "redundant with"

DEFAULT_REDUNDANCY_THRESHOLD = 0.0


def redundant_reason(other_slug: str) -> str:
    """The `Dropped.reason` for a lesson that duplicates `other_slug`."""
    return f"{REASON_REDUNDANT} {other_slug}"


@dataclass(frozen=True)
class Budget:
    max_lessons: int = DEFAULT_MAX_LESSONS
    max_chars: int = DEFAULT_MAX_CHARS
    redundancy_threshold: float = DEFAULT_REDUNDANCY_THRESHOLD

    def __post_init__(self) -> None:
        if self.max_lessons < 0 or self.max_chars < 0:
            raise ValueError(
                "a budget cannot be negative; use 0 to inject nothing "
                f"(got max_lessons={self.max_lessons}, max_chars={self.max_chars})"
            )
        if not 0.0 <= self.redundancy_threshold <= 1.0:
            raise ValueError(
                "redundancy_threshold must be between 0.0 (off) and 1.0 "
                f"(got {self.redundancy_threshold})"
            )


@dataclass(frozen=True)
class Candidate:
    """One lesson considered for injection, with the size it would cost."""

    slug: str
    text: str
    core: bool = False
    relevance: float = 0.0
    importance: int = 0
    revision: str = ""
    compare_text: str = ""

    @property
    def cost(self) -> int:
        return len(self.text)

    @property
    def comparable(self) -> str:
        """What this candidate is compared against others on."""
        return self.compare_text or self.text


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
    noted: tuple[Dropped, ...] = field(default_factory=tuple)
    chars_used: int = 0
    budget: Budget = field(default_factory=Budget)

    @property
    def core_admitted(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.admitted if c.core)

    @property
    def core_dropped(self) -> tuple[Dropped, ...]:
        return tuple(d for d in self.dropped if d.core)

    @property
    def redundant_dropped(self) -> tuple[Dropped, ...]:
        """Lessons left out for restating one that was admitted."""
        return tuple(d for d in self.dropped if d.reason.startswith(REASON_REDUNDANT))

    @property
    def redundant_core(self) -> tuple[Dropped, ...]:
        """Core lessons that duplicate another admitted lesson."""
        return tuple(
            d for d in self.noted if d.core and d.reason.startswith(REASON_REDUNDANT)
        )

    @property
    def chars_available(self) -> int:
        return max(0, self.budget.max_chars - self.chars_used)

    def gauge(self, also_left_out: int = 0) -> str:
        pct = (
            (self.chars_used / self.budget.max_chars * 100)
            if self.budget.max_chars else 0.0
        )
        line = (
            f"{len(self.admitted)}/{self.budget.max_lessons} lessons, "
            f"{self.chars_used:,}/{self.budget.max_chars:,} chars ({pct:.0f}%)"
        )
        if self.dropped or also_left_out:
            line += f", {len(self.dropped) + also_left_out} not injected"
        return line


def select(candidates, budget: Budget | None = None) -> Dose:
    """Admit as much as the budget allows: core first, then by rank."""
    budget = budget or Budget()
    items = list(candidates)

    core = sorted(
        (c for c in items if c.core),
        key=lambda c: (-int(c.importance or 0), c.slug),
    )
    matched = [c for c in items if not c.core]

    admitted: list[Candidate] = []
    dropped: list[Dropped] = []
    noted: list[Dropped] = []
    used = 0

    check_redundancy = budget.redundancy_threshold > 0
    admitted_tokens: list[tuple[str, frozenset[str]]] = []
    token_set = None
    jaccard = None
    if check_redundancy:
        from commontrace.redundancy import jaccard, token_set  # noqa: PLC0415

    for group in (core, matched):
        for candidate in group:
            if len(admitted) >= budget.max_lessons:
                dropped.append(Dropped(candidate.slug, REASON_COUNT, candidate.core))
                continue

            duplicate_of = ""
            if check_redundancy:
                assert token_set is not None and jaccard is not None
                tokens = token_set(candidate.comparable)
                for other_slug, other_tokens in admitted_tokens:
                    if jaccard(tokens, other_tokens) >= budget.redundancy_threshold:
                        duplicate_of = other_slug
                        break
                if duplicate_of and not candidate.core:
                    dropped.append(
                        Dropped(candidate.slug, redundant_reason(duplicate_of), False)
                    )
                    continue

            if used + candidate.cost > budget.max_chars:
                dropped.append(Dropped(candidate.slug, REASON_CHARS, candidate.core))
                continue

            admitted.append(candidate)
            used += candidate.cost
            if check_redundancy:
                assert token_set is not None
                admitted_tokens.append((candidate.slug, token_set(candidate.comparable)))
                if duplicate_of:
                    noted.append(
                        Dropped(candidate.slug, redundant_reason(duplicate_of), True)
                    )

    return Dose(
        admitted=tuple(admitted),
        dropped=tuple(dropped),
        noted=tuple(noted),
        chars_used=used,
        budget=budget,
    )


def is_core(fm: dict) -> bool:
    """Whether a lesson's frontmatter marks it always-on."""
    raw = fm.get(CORE_FIELD, False)
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "yes", "1", "on")
    return bool(raw)
