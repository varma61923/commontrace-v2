"""How much memory to inject, and which memory is always injected."""

from __future__ import annotations

import os
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
    """Hard context caps; zero count or characters delivers no optional lessons."""
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
    hazard: bool = False

    @property
    def cost(self) -> int:
        return len(self.text)

    @property
    def comparable(self) -> str:
        """What this candidate is compared against others on."""
        return self.compare_text or self.text


@dataclass(frozen=True)
class Dropped:
    """A candidate excluded by count, context size or redundancy checks."""
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
    harm_budget: dict = field(default_factory=dict)
    packing: dict = field(default_factory=dict)

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


def select(candidates, budget: Budget | None = None, *, root: str | None = None, context: str = "",
           scorer: str | None = None) -> Dose:
    """Admit as much as the budget allows: core first, then by rank.

    With a store ``root``, reviewed hazards are flagged and always admitted, an
    operator-enabled harm budget (`conformal_gate`) filters discretionary
    memories, and opt-in value-per-token packing replaces rank order. ``context``
    is the occasion context exactly as the holdout log records it, and
    ``scorer`` the eligibility label whose relevance scale the candidates use.
    """
    budget = budget or Budget()
    items = list(candidates)
    packing: dict = {}
    harm_budget: dict = {}
    gate_dropped: list[Dropped] = []
    if root is not None:
        from dataclasses import replace

        from commontrace import conformal_gate, holdout_io, safety

        try:
            context = holdout_io.context_label(context)
        except ValueError:
            context = ""
        hazards = safety.protected(root)
        items = [replace(item, hazard=True) if item.slug in hazards else item for item in items]
        gate = conformal_gate.current(root)
        harm_budget = conformal_gate.summary(gate, context)
        if gate.get("enabled"):
            kept = []
            for candidate in items:
                # Core memories are operator-pinned and never randomized, so no
                # calibration covers them; the budget governs discretionary memory.
                if candidate.core or conformal_gate.admit(
                        gate, conformal_gate.score(candidate.relevance), context=context,
                        hazard=candidate.hazard, scorer=scorer):
                    kept.append(candidate)
                else:
                    gate_dropped.append(Dropped(candidate.slug, "harm-budget gate", candidate.core))
            items = kept
        options = _packing_options(root)
        if options.get("enabled"):
            from commontrace import frontier_protocol as fp

            for item in items:
                fp.number(item.relevance, -1e6, 1e6, "relevance")
            priors = options.get("contexts", {}).get(context, {})
            values = {item.slug: priors.get(item.slug, options.get("lessons", {}).get(item.slug, 0))
                      * max(0, min(1, item.relevance)) for item in items}
            selected, packing = value_pack(items, values, max_tokens=budget.max_chars // 4,
                                           max_items=budget.max_lessons)
            picked = {item.slug for item in selected}
            gate_dropped.extend(Dropped(item.slug, "causal value-per-token packing", item.core)
                                for item in items if item.slug not in picked)
            items = selected

    core = sorted(
        (c for c in items if c.core or c.hazard),
        key=lambda c: (-int(c.importance or 0), c.slug),
    )
    matched = [c for c in items if not c.core and not c.hazard]

    admitted: list[Candidate] = []
    dropped: list[Dropped] = gate_dropped
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
            if candidate.hazard:
                admitted.append(candidate)
                used += candidate.cost
                continue
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
        harm_budget=harm_budget,
        packing=packing,
    )


def is_core(fm: dict) -> bool:
    """Whether a lesson's frontmatter marks it always-on."""
    raw = fm.get(CORE_FIELD, False)
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "yes", "1", "on")
    return bool(raw)


def value_pack(candidates: list[Candidate], values: dict[str, float], *, max_tokens: int,
               max_items: int = 10, duplicate_penalty: float = .5) -> tuple[list[Candidate], dict]:
    """Bounded knapsack with a relevance-baseline fallback and mandatory hazards.

    Values are posterior uplift times relevance, hence estimates, not guarantees
    of realized task improvement. Four-character token estimates are labelled.
    Duplicate adjustment is a fixed additive surrogate; baseline dominance is
    with respect to exactly that same declared objective.
    """
    from commontrace import frontier_protocol as fp
    from commontrace.redundancy import jaccard, token_set

    fp.count(len(candidates), 0, 256)
    fp.count(max_tokens, 0, 250_000)
    fp.count(max_items, 0, 256)
    fp.number(duplicate_penalty, 0, 1)
    mandatory = [item for item in candidates if item.hazard or item.core]
    optional = [item for item in candidates if not item.hazard and not item.core]
    def weight(item: Candidate) -> int:
        return max(1, (len(item.text) + 3) // 4)
    capacity = max(0, max_tokens - sum(weight(item) for item in mandatory))
    slots = max(0, max_items - len(mandatory))
    utilities, prior_terms = {}, []
    for item in optional:
        value = fp.number(values.get(item.slug, 0), -1, 1, "posterior uplift times relevance")
        terms = token_set(item.comparable[:4096])
        duplicate = max((jaccard(terms, before) for before in prior_terms), default=0)
        utilities[item.slug] = value * (1 - duplicate_penalty * duplicate)
        prior_terms.append(terms)
    baseline, spent = [], 0
    for item in optional:
        if len(baseline) < slots and spent + weight(item) <= capacity:
            baseline.append(item)
            spent += weight(item)
    states: dict[tuple[int, int], tuple[float, tuple[int, ...]]] = {(0, 0): (0, ())}
    operations, capped = 0, False
    for index, item in enumerate(optional):
        for (used, count), (utility, picked) in list(states.items()):
            operations += 1
            if operations > 500_000 or len(states) > 30_000:
                capped = True
                break
            key = (used + weight(item), count + 1)
            candidate_value = utility + utilities[item.slug]
            if key[0] <= capacity and key[1] <= slots and candidate_value > states.get(key, (-1, ()))[0]:
                states[key] = candidate_value, (*picked, index)
        if capped:
            break
    best_value, picked = max(states.values(), key=lambda state: (state[0], -len(state[1])))
    baseline_value = sum(utilities[item.slug] for item in baseline)
    chosen = baseline if baseline_value >= best_value else [optional[i] for i in picked]
    return [*mandatory, *chosen], {"method": "bounded knapsack with baseline dominance", "capped": capped,
                                  "estimated_value": max(best_value, baseline_value),
                                  "baseline_value": baseline_value, "tokenizer": "four-character estimate"}


_PACKING: dict[str, tuple[tuple[int, int, int], dict]] = {}


def _packing_options(root: str) -> dict:
    """The packing snapshot, re-read only when its file changes (no hot-path parse)."""
    import json

    from commontrace import frontier_protocol as fp

    path = fp.filename(root, "context-packing.json")
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return {}
    stamp = (info.st_ino, info.st_mtime_ns, info.st_size)
    cached = _PACKING.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    with open(path, "rb") as stream:
        raw = stream.read(fp.MAX_BYTES + 1)
    if len(raw) > fp.MAX_BYTES:
        raise ValueError("packing configuration exceeds 1 MiB")
    options = json.loads(raw)
    if not isinstance(options, dict):
        raise ValueError("packing configuration must be an object")
    if len(_PACKING) > 64:
        _PACKING.clear()
    _PACKING[path] = (stamp, options)
    return options


def configure_value_packing(root: str) -> dict:
    """Snapshot causal-gate priors off the retrieval hot path; refresh explicitly."""
    from commontrace import _jsonl, causal_gate
    from commontrace import frontier_protocol as fp

    rows = causal_gate.current_rows(root)
    model = causal_gate.fit(rows, causal_gate.sources_for(root, {row.lesson for row in rows}))
    contexts: dict[str, dict[str, float]] = {}
    for (lesson, context), posterior in model.contexts.items():
        contexts.setdefault(context, {})[lesson] = posterior.mean
    body = {"enabled": True, "lessons": {lesson: posterior.mean for lesson, posterior in model.lessons.items()},
            "contexts": contexts, "calibrated_at": fp.now()}
    _jsonl.write_json(fp.filename(root, "context-packing.json"), body)
    return body
