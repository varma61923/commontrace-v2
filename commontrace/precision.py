"""How often does the outcome detector get the occasion right? Measured on a labelled sample.

`outcome_detect` turns signals a system already has into a success or a failure, or says it cannot
tell yet. Whether that reading agrees with a person who looked at the case is a measurement, not a
design property, and it is the number that decides whether the verdicts built on those outcomes can
be trusted. This scores it.

A sample is JSON lines, one per occasion a person has labelled:

    {"occasion_id": "t-1", "truth": true, "combine": "all",
     "signals": [{"detector": "from_csat", "args": {"score": 5, "scale_max": 5}},
                 {"detector": "from_human_takeover", "args": {"human_took_over": false}}]}

`signals` and `combine` are exactly what `POST /v1/outcome` and a function kit use. `truth` is the
person's answer: did the task succeed.

Reported:
  precision          of the occasions the detector called SUCCESS, the share that truly succeeded.
                     The figure that matters: a false success reports a win that did not happen.
  failure precision  of those it called FAILURE, the share that truly failed.
  decided            the share it answered at all. An undecided occasion records nothing, which is
                     the correct response to a window still open, and costs speed, not correctness.
  recall             of the truly successful, the share it called success.

A sample taken from the same fleet and labelled by someone other than whoever configured the
detectors is evidence; a sample the detector's author wrote is a regression suite for the detector's
stated rules, and is described that way wherever it is reported.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field


class SampleError(ValueError):
    """The labelled sample cannot be read."""


@dataclass
class Result:
    n: int = 0
    decided: int = 0
    true_positive: int = 0
    false_positive: int = 0
    true_negative: int = 0
    false_negative: int = 0
    undecided_successes: int = 0
    wrong: list = field(default_factory=list)

    @property
    def called_success(self) -> int:
        return self.true_positive + self.false_positive

    @property
    def called_failure(self) -> int:
        return self.true_negative + self.false_negative

    @property
    def precision(self) -> float | None:
        return self.true_positive / self.called_success if self.called_success else None

    @property
    def failure_precision(self) -> float | None:
        return self.true_negative / self.called_failure if self.called_failure else None

    @property
    def decided_share(self) -> float | None:
        return self.decided / self.n if self.n else None

    @property
    def recall(self) -> float | None:
        successes = self.true_positive + self.false_negative + self.undecided_successes
        return self.true_positive / successes if successes else None

    def to_dict(self) -> dict:
        out = dataclasses.asdict(self)
        out.update(precision=self.precision, failure_precision=self.failure_precision,
                   decided_share=self.decided_share, recall=self.recall)
        return out


def read_sample(text: str) -> list[dict]:
    rows = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise SampleError(f"line {number}: not JSON ({exc})") from None
        if not isinstance(row, dict) or not isinstance(row.get("truth"), bool):
            raise SampleError(f"line {number}: needs a boolean \"truth\"")
        if not isinstance(row.get("signals"), list) or not row["signals"]:
            raise SampleError(f"line {number}: needs a non-empty \"signals\" list")
        rows.append(row)
    if not rows:
        raise SampleError("the sample is empty")
    return rows


def evaluate(rows: list[dict]) -> Result:
    """Run each row's signals through the detectors the gateway runs, and score against `truth`."""
    from commontrace import gateway

    result = Result()
    for row in rows:
        result.n += 1
        try:
            predicted = gateway.evaluate_signals(row["signals"], row.get("combine"))
        except gateway.ApiError as exc:
            raise SampleError(f"{row.get('occasion_id', '?')}: {exc.message}") from None
        truth = row["truth"]
        if predicted is None:
            if truth:
                result.undecided_successes += 1
            continue
        result.decided += 1
        if predicted and truth:
            result.true_positive += 1
        elif predicted and not truth:
            result.false_positive += 1
            result.wrong.append({"occasion_id": row.get("occasion_id"), "called": "success", "truth": False})
        elif not predicted and not truth:
            result.true_negative += 1
        else:
            result.false_negative += 1
            result.wrong.append({"occasion_id": row.get("occasion_id"), "called": "failure", "truth": True})
    return result


def render(result: Result, *, min_precision: float) -> str:
    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{x:.1%}"

    lines = [
        f"{result.n} labelled occasions; the detector decided {result.decided} ({pct(result.decided_share)}).",
        f"  precision of SUCCESS calls  {pct(result.precision)}  ({result.true_positive} of {result.called_success})"
        f"   target >= {min_precision:.0%}",
        f"  precision of FAILURE calls  {pct(result.failure_precision)}  "
        f"({result.true_negative} of {result.called_failure})",
        f"  recall of true successes    {pct(result.recall)}",
    ]
    for w in result.wrong[:10]:
        truth = "success" if w["truth"] else "failure"
        lines.append(f"  wrong: {w['occasion_id']} called {w['called']}, truth {truth}")
    return "\n".join(lines)
