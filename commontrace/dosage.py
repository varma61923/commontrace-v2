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

WHY A THIRD BUDGET: REDUNDANCY
------------------------------
Count and characters bound how much the agent receives. Neither bounds how
much of it is DISTINCT. Two lessons that restate each other rank highly for
the same task by construction -- that is what makes them restatements -- so
both were admitted, and the second one spent a slot and its characters to
tell the agent something it had already been told. A store with three
redundant pairs and `max_lessons=10` is injecting seven lessons' worth of
guidance and reporting ten.

So a budget may also carry a `redundancy_threshold`: a candidate that says
substantially the same thing as one already admitted is dropped with the
slug it duplicates, and the freed slot goes to the next DISTINCT lesson
instead. Similarity is `commontrace/redundancy.py`'s token-set Jaccard --
the same lexical vocabulary the local retriever ranks in, for the reason
that module's docstring gives.

This is the same job Graphiti's `maximal_marginal_relevance` reranker does
for a knowledge graph, and it is deliberately NOT the same mechanism. MMR
re-scores every candidate as `λ·relevance - (1-λ)·max_similarity` and
re-sorts, which is a second opinion about relevance layered on top of the
retriever's. `select` refuses to do that on purpose (see its docstring):
retrieval already ranked, and re-sorting here would mean two different
answers to "what is most relevant" in one pipeline, with only one of them
visible to the person reading `commontrace query`. Redundancy is applied
as a HARD GATE in rank order instead -- nothing is promoted, nothing is
demoted, and the only thing that can happen to a lesson is being dropped
for naming the lesson it duplicates. That is auditable in a way a blended
score is not.

**Default off.** A store that has been injecting ten lessons and starts
injecting seven has had its treatment changed, and if a holdout experiment
is running, the arms before and after are not the same intervention. This
is the same reasoning that keeps `fusion` off by default in
`commontrace/retrieval_io.py`: opting in is a decision, not an upgrade
side effect.

**Core lessons are never suppressed.** A core lesson is the fleet's
unconditional position, and "you did not receive the fleet's rule because
something that matched today's vocabulary resembled it" is not a trade
anyone asked for. Two core lessons that duplicate each other are a
configuration error, reported the way an over-budget core set is reported
(`Dose.redundant_core`) rather than silently resolved.

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
#: Prefix of the reason recorded when a lesson is dropped for saying the
#: same thing as one already admitted. The duplicated slug is appended, so
#: the reason a reader sees names what displaced it -- see
#: `redundant_reason`.
REASON_REDUNDANT = "redundant with"

#: A store that has not configured redundancy suppression gets none. See the
#: module docstring for why this is off rather than on.
DEFAULT_REDUNDANCY_THRESHOLD = 0.0


def redundant_reason(other_slug: str) -> str:
    """The `Dropped.reason` for a lesson that duplicates `other_slug`."""
    return f"{REASON_REDUNDANT} {other_slug}"


@dataclass(frozen=True)
class Budget:
    max_lessons: int = DEFAULT_MAX_LESSONS
    max_chars: int = DEFAULT_MAX_CHARS
    #: Similarity at or above which a candidate is considered to restate one
    #: already admitted, and is dropped. 0 disables the check entirely,
    #: which is the default. See the module docstring.
    redundancy_threshold: float = DEFAULT_REDUNDANCY_THRESHOLD

    def __post_init__(self) -> None:
        if self.max_lessons < 0 or self.max_chars < 0:
            raise ValueError(
                "a budget cannot be negative; use 0 to inject nothing "
                f"(got max_lessons={self.max_lessons}, max_chars={self.max_chars})"
            )
        # A threshold above 1.0 can never fire (Jaccard is bounded by 1), and
        # a negative one fires on every pair including two lessons that share
        # no vocabulary at all -- which would drop everything after the first
        # admission and look, from the outside, exactly like retrieval
        # breaking. Rejected rather than clamped: both are configuration
        # mistakes, and silently reinterpreting a store's stated setting is
        # how a store ends up serving a treatment nobody chose.
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
    #: True for a lesson the store marks as always-on.
    core: bool = False
    #: Ranking position from retrieval, for the matched set. Ignored for core.
    relevance: float = 0.0
    importance: int = 0
    revision: str = ""
    #: The text redundancy is judged on, when it differs from what the agent
    #: receives. Empty means "use `text`", which is what a caller holding
    #: only the rendered lesson should do. A caller that also holds the
    #: frontmatter passes `redundancy.comparable_text(fm, body)` here, so
    #: two lessons are compared on the same fields
    #: `commontrace consolidate` and the authoring check compare them on --
    #: one definition of "says the same thing" across all three surfaces.
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
    #: Things worth telling an operator that did NOT change admission.
    #: Currently one kind: a core lesson that duplicates another admitted
    #: lesson, which is reported and still injected (see the module
    #: docstring on why core is never suppressed). Kept separate from
    #: `dropped` so no caller can mistake a note for a lesson the agent did
    #: not receive.
    noted: tuple[Dropped, ...] = field(default_factory=tuple)
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
    def redundant_dropped(self) -> tuple[Dropped, ...]:
        """Lessons left out for restating one that was admitted.

        Separated from the budget drops because they mean something
        different to an operator: a count/character drop says "buy more
        budget", a redundancy drop says "you have two lessons saying one
        thing, go merge them" -- which is what `commontrace consolidate`
        is for.
        """
        return tuple(d for d in self.dropped if d.reason.startswith(REASON_REDUNDANT))

    @property
    def redundant_core(self) -> tuple[Dropped, ...]:
        """Core lessons that duplicate another admitted lesson.

        Never suppressed -- see the module docstring -- so these are
        REPORTED while still being admitted. A non-empty tuple here is a
        configuration error in the always-on set, the same class of problem
        as `core_dropped`.
        """
        return tuple(
            d for d in self.noted if d.core and d.reason.startswith(REASON_REDUNDANT)
        )

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

    When the budget carries a `redundancy_threshold`, a candidate that
    restates one already admitted is dropped naming it, and the slot passes
    to the next distinct candidate. The check runs BEFORE the character
    test: a redundant lesson that also would not have fit should be
    reported as redundant, because that is the fact an operator can act on.
    Core lessons are checked but never suppressed -- see the module
    docstring.
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
    noted: list[Dropped] = []
    used = 0

    # Tokenized lazily and only when the check is on, so a store that has
    # not opted in pays nothing for this at all -- `select` runs on every
    # retrieval.
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
                assert token_set is not None and jaccard is not None  # for type checkers
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
                assert token_set is not None  # for type checkers
                admitted_tokens.append((candidate.slug, token_set(candidate.comparable)))
                if duplicate_of:
                    # Admitted anyway (core), but the operator should know
                    # the always-on set contains two lessons saying one thing.
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
    """Whether a lesson's frontmatter marks it always-on.

    Tolerant of the shapes a hand-edited YAML file produces ("true", "yes",
    True) and of the field being absent, which is the overwhelmingly common
    case: a store that has never heard of core lessons has none.
    """
    raw = fm.get(CORE_FIELD, False)
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "yes", "1", "on")
    return bool(raw)
