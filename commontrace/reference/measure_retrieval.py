#!/usr/bin/env python3
"""measure_retrieval.py — is retrieval equally good in every field?

The product claims one loop works for a coding fleet, an HR fleet, a legal
fleet, a robotics fleet, or a field nobody has thought of yet. Retrieval is
where that claim quietly failed, and it failed in a way a single aggregate
number cannot show: scoring was raw word overlap with no normalization, so
fields that write more (legal, robotics) produced systematically higher scores
than terse ones (coding), and every threshold meant something different in
each store. Measured before the fix, collateral retrievals ranged from 16
(robotics) to 27 (legal) across the original six fields — a mean would have
called that "about 21" and hidden the spread entirely.

So this reports PER FIELD and gates on two things, neither of which is the
mean:

- the WORST field's pollution, absolutely. A mean lets a change that improves
  one field and regresses another look like progress.
- the SPREAD between the worst and best field. This is the field-agnosticism
  question specifically: is retrieval materially worse in one field than
  another?

Both are needed, and building this proved it. Against the historical scorer
the eight fields pollute at 1.72x-2.50x; against the shipped scorer,
1.72x-2.33x. Tuning the floor against THIS corpus alone would buy far more
(1.00x-1.28x at floor=0.10), but a second corpus bounds how high the floor
can go -- see DEFAULT_FLOOR in commontrace/retrieval.py and benchmark/
STATUS.md §9.6, which records why it was lowered to 0.04 after shipping.
The SPREAD barely moves either way (1.45x to 1.36x), because the old scorer
was bad in every field roughly equally. A spread-only gate would have called
that regression acceptable.
Conversely a ceiling-only gate passes a change that fixes five fields and
abandons the sixth, which is the failure this whole file exists for.

Metrics, per field:

- precision@k / recall@k / MRR : ordinary retrieval quality against labelled
  queries (commontrace/fixtures/fields/*.json).
- collateral@k : retrieved lessons that are NOT relevant to the query. Not a
  cosmetic count. Under `query --experiment` every retrieved lesson is logged
  as an eligible holdout assignment, so each collateral retrieval attributes
  an unrelated task's outcome to a lesson that had nothing to do with it. That
  is the mechanism that produced a lesson with 246 assignments against ~80
  real occasions and a significant-but-spurious HURTS verdict.
- pollution_ratio : assignments a fleet would log divided by the ones actually
  about the lesson — the 246/80 = 3.1 above, expressed so it can be tracked.

Usage:
    python measure_retrieval.py                 # markdown to stdout
    python measure_retrieval.py --json          # raw JSON
    python measure_retrieval.py --fixtures DIR  # override the corpus
    python measure_retrieval.py --max-spread 2  # non-zero exit if exceeded
"""
import argparse
import glob
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from commontrace import retrieval  # noqa: E402

SCHEMA_VERSION = "1.0.0"

# The corpus ships inside the package (pyproject.toml package-data), so this
# one join resolves in both environments without special-casing: `_REPO` is the
# package's PARENT, which is the repo root in a checkout and site-packages in
# an install. Keeping the corpus out of the wheel would have reproduced the
# exact failure that made semantic retrieval unreachable -- a command that only
# works from a repo checkout, for a claim ("retrieval works as well in your
# field as in ours") a customer should be able to re-run themselves.
DEFAULT_FIXTURES = os.path.join(_REPO, "commontrace", "fixtures", "fields")


def load_fields(fixtures_dir):
    fields = []
    for path in sorted(glob.glob(os.path.join(fixtures_dir, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            fields.append(json.load(fh))
    return fields


def _as_ranker_input(lessons):
    """The (path, frontmatter) shape rank_lessons takes, same as a real store."""
    return [(f"<fixture>/{lesson['name']}.md", dict(lesson)) for lesson in lessons]


def measure_field(doc, top_k=3, floor=None, scorer=retrieval.SCORER_IDF):
    lessons = _as_ranker_input(doc["lessons"])
    n_q = 0
    hits_at_1 = 0
    found = 0
    collateral = 0
    reciprocal = 0.0
    retrieved_total = 0

    for case in doc["queries"]:
        relevant = set(case["relevant"])
        ranked = retrieval.rank_lessons(
            case["query"], lessons, top_k=top_k, floor=floor, scorer=scorer)
        slugs = [r.slug for r in ranked]
        n_q += 1
        retrieved_total += len(slugs)
        if slugs and slugs[0] in relevant:
            hits_at_1 += 1
        if relevant & set(slugs):
            found += 1
        collateral += len([s for s in slugs if s not in relevant])
        for i, slug in enumerate(slugs, start=1):
            if slug in relevant:
                reciprocal += 1.0 / i
                break

    return {
        "field": doc["field"],
        "n_lessons": len(doc["lessons"]),
        "n_queries": n_q,
        "precision_at_1": hits_at_1 / n_q if n_q else None,
        "recall_at_k": found / n_q if n_q else None,
        "mrr": reciprocal / n_q if n_q else None,
        "collateral_at_k": collateral,
        # How many assignments a fleet would log per assignment that is
        # actually about the lesson. 1.0 is clean; the observed failure was
        # roughly 3.1.
        "pollution_ratio": (retrieved_total / found) if found else None,
    }


def compute(fixtures_dir=DEFAULT_FIXTURES, top_k=3, floor=None,
            scorer=retrieval.SCORER_IDF):
    fields = [measure_field(doc, top_k=top_k, floor=floor, scorer=scorer)
              for doc in load_fields(fixtures_dir)]
    ratios = [f["pollution_ratio"] for f in fields if f["pollution_ratio"]]
    worst = max(ratios) if ratios else None
    best = min(ratios) if ratios else None
    return {
        "schema_version": SCHEMA_VERSION,
        "scorer": scorer,
        "floor": retrieval.default_floor(scorer) if floor is None else floor,
        "top_k": top_k,
        "fields": fields,
        # The gate. A mean would let a change that helps one field and hurts
        # another look like an improvement; this cannot.
        "pollution_spread": (worst / best) if (worst and best) else None,
        "worst_field": max(fields, key=lambda f: f["pollution_ratio"] or 0)["field"] if fields else None,
        "mean_precision_at_1": (
            sum(f["precision_at_1"] or 0 for f in fields) / len(fields) if fields else None
        ),
    }


def _fmt(value, spec=".2f"):
    return "N/A" if value is None else format(value, spec)


def render_markdown(report):
    out = [
        "# Cross-field retrieval",
        "",
        f"**Scorer** : `{report['scorer']}` · **floor** : {report['floor']} · "
        f"**top-k** : {report['top_k']}",
        "",
        "| Field | Lessons | Queries | P@1 | Recall@k | MRR | Collateral | Pollution |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for f in report["fields"]:
        out.append(
            f"| {f['field']} | {f['n_lessons']} | {f['n_queries']} | "
            f"{_fmt(f['precision_at_1'], '.0%')} | {_fmt(f['recall_at_k'], '.0%')} | "
            f"{_fmt(f['mrr'])} | {f['collateral_at_k']} | {_fmt(f['pollution_ratio'])}× |"
        )
    out += [
        "",
        f"**Pollution spread (worst ÷ best field)** : {_fmt(report['pollution_spread'])}× "
        f"— worst is `{report['worst_field']}`.",
        "",
        "Pollution is assignments logged per assignment actually about the lesson. "
        "It is the quantity that turns retrieval imprecision into a wrong causal "
        "verdict, because `query --experiment` logs an eligible holdout assignment "
        "for every retrieved lesson.",
        "",
        "The spread, not the mean, is what this gates on: a change that improves one "
        "field and regresses another can improve an average.",
    ]
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fixtures", default=DEFAULT_FIXTURES)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--floor", type=float, default=None)
    parser.add_argument("--scorer", default=retrieval.SCORER_IDF,
                        choices=list(retrieval.LEXICAL_SCORERS))
    parser.add_argument(
        "--max-spread", type=float, default=None,
        help="Exit non-zero if the worst field's pollution ratio exceeds the best "
             "field's by more than this multiple -- retrieval being materially worse "
             "in one field than another is what 'works for any agent type' cannot mean.",
    )
    parser.add_argument(
        "--max-pollution", type=float, default=None,
        help="Exit non-zero if ANY field's pollution ratio exceeds this. Needed "
             "alongside --max-spread: a scorer that is equally bad everywhere has an "
             "excellent spread.",
    )
    parser.add_argument("--dest", default=None, help="Ignored; accepted for `bench` parity.")
    args = parser.parse_args()

    report = compute(args.fixtures, top_k=args.top_k, floor=args.floor, scorer=args.scorer)
    print(json.dumps(report, indent=2) if args.json else render_markdown(report))

    failed = False
    if args.max_pollution is not None:
        over = [
            f for f in report["fields"]
            if f["pollution_ratio"] is not None and f["pollution_ratio"] > args.max_pollution
        ]
        if over:
            names = ", ".join(f"{f['field']}={f['pollution_ratio']:.2f}x" for f in over)
            print(
                f"\n[FAIL] pollution exceeds {args.max_pollution}x in: {names}. Every "
                "retrieval above what the query is actually about becomes an eligible "
                "holdout assignment, so this is measurement error entering the causal "
                "estimate, not just noise in the output.",
                file=sys.stderr,
            )
            failed = True

    if args.max_spread is not None and report["pollution_spread"] is not None:
        if report["pollution_spread"] > args.max_spread:
            print(
                f"\n[FAIL] pollution spread {report['pollution_spread']:.2f}x exceeds "
                f"{args.max_spread}x -- retrieval is materially worse in "
                f"'{report['worst_field']}' than in the best field.",
                file=sys.stderr,
            )
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
