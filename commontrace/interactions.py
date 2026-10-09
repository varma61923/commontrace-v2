"""Do two lessons help or hurt each other? A factorial reading of independent arms.

Each (lesson, occasion) is assigned by its own hash of (salt, lesson, occasion),
so on occasions where two lessons were both eligible, their injected/withheld
states form a randomized 2x2 design for free. Eligibility is decided by
retrieval before assignment, so restricting to co-eligible occasions keeps the
comparison randomized.

For lessons A and B, with cell success rates y[a][b] (1 = injected):

    interaction = (y11 - y01) - (y10 - y00)

the change in A's effect when B is also present. Positive is ``SYNERGY``
(they work better together), negative is ``INTERFERENCE`` (one undoes or
contradicts the other, a sign the pair should be merged, ordered or scoped
apart). The four cells are independent binomials, so the variance is the sum
of the cell variances; tests are Benjamini-Hochberg corrected across every
pair read. Like subgroup effects, this is an exploratory fixed-horizon
reading, never the billing verdict.
"""
from __future__ import annotations

import itertools
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from commontrace import experiment

DEFAULT_MIN_CELL = 10
MAX_PAIRS = 200
FLAG_SYNERGY = "SYNERGY"
FLAG_INTERFERENCE = "INTERFERENCE"
FLAG_ADDITIVE = "ADDITIVE"


@dataclass
class PairInteraction:
    lesson_a: str
    lesson_b: str
    cells: dict  # "00","01","10","11" -> [successes, n]
    effect_a_without_b: float
    effect_a_with_b: float
    interaction: float
    ci_low: float
    ci_high: float
    p_value: float
    flag: str = FLAG_ADDITIVE
    significant: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _rate_var(s: int, n: int) -> tuple[float, float]:
    p = s / n
    # A half-count floor keeps a 0% or 100% cell's variance finite and conservative.
    q = min(max(p, 0.5 / n), 1 - 0.5 / n)
    return p, q * (1 - q) / n


def _normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def analyze(observations: Iterable[experiment.HoldoutObservation], *, min_cell: int = DEFAULT_MIN_CELL,
            alpha: float = 0.05, max_pairs: int = MAX_PAIRS) -> list[PairInteraction]:
    """Interactions for every co-eligible lesson pair with `min_cell` outcomes in each of its four cells."""
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between 0 and 1")
    if min_cell < 2:
        raise ValueError("min_cell must be at least 2")
    by_occasion: dict[str, dict[str, bool]] = defaultdict(dict)
    outcome: dict[str, bool] = {}
    for obs in observations:
        by_occasion[obs.occasion_id][obs.lesson_slug] = obs.injected
        outcome[obs.occasion_id] = obs.succeeded
    co = defaultdict(int)
    for arms in by_occasion.values():
        for a, b in itertools.combinations(sorted(arms), 2):
            co[(a, b)] += 1
    # The most frequently co-eligible pairs first; a cap bounds the multiple-testing burden.
    pairs = sorted(co, key=lambda k: (-co[k], k))[:max_pairs]
    staged: list[PairInteraction] = []
    for a, b in pairs:
        cells = {k: [0, 0] for k in ("00", "01", "10", "11")}
        for occasion, arms in by_occasion.items():
            if a in arms and b in arms:
                key = f"{int(arms[a])}{int(arms[b])}"
                cells[key][0] += int(outcome[occasion])
                cells[key][1] += 1
        if any(n < min_cell for _s, n in cells.values()):
            continue
        (y00, v00), (y01, v01), (y10, v10), (y11, v11) = (_rate_var(*cells[k]) for k in ("00", "01", "10", "11"))
        interaction = (y11 - y01) - (y10 - y00)
        se = math.sqrt(v00 + v01 + v10 + v11)
        z = abs(interaction) / se
        staged.append(PairInteraction(
            a, b, cells, round(y10 - y00, 4), round(y11 - y01, 4), round(interaction, 4),
            round(interaction - 1.959964 * se, 4), round(interaction + 1.959964 * se, 4),
            round(min(1.0, 2 * _normal_sf(z)), 6)))
    for pair, significant in zip(staged, experiment.benjamini_hochberg([p.p_value for p in staged], alpha=alpha)):
        pair.significant = significant
        if significant:
            pair.flag = FLAG_SYNERGY if pair.interaction > 0 else FLAG_INTERFERENCE
    rank = {FLAG_INTERFERENCE: 0, FLAG_SYNERGY: 1, FLAG_ADDITIVE: 2}
    staged.sort(key=lambda p: (rank[p.flag], p.p_value, p.lesson_a, p.lesson_b))
    return staged


def render(results: list[PairInteraction]) -> str:
    lines = ["Lesson interactions on co-eligible occasions (exploratory, fixed-horizon; BH across pairs; "
             "not the billing verdict)", ""]
    if not results:
        lines.append("No lesson pair has enough co-eligible outcomes in all four injected/withheld cells yet.")
        return "\n".join(lines)
    for p in results:
        mark = "*" if p.significant else " "
        lines.append(f"{mark} {p.lesson_a} x {p.lesson_b}: {p.flag}  interaction {p.interaction:+.1%} "
                     f"[{p.ci_low:+.1%}, {p.ci_high:+.1%}]  (A's effect {p.effect_a_without_b:+.1%} alone, "
                     f"{p.effect_a_with_b:+.1%} with B; n={sum(n for _s, n in p.cells.values())})")
        if p.flag == FLAG_INTERFERENCE:
            lines.append("    -> they work against each other together: merge them, order them, or scope one away.")
    lines.append("")
    lines.append("* = significant after correction. Interaction = A's effect with B minus A's effect without B.")
    return "\n".join(lines)
