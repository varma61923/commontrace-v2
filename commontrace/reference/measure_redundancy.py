#!/usr/bin/env python3
"""measure_redundancy.py — where is the near-duplicate threshold quiet?

`commontrace/redundancy.py` decides whether two lessons say the same thing,
and every consumer of that answer (the `consolidate` report, the
authoring-time duplicate check, `dosage`'s optional injection-time
suppression) inherits its threshold. A threshold picked by taste would be
wrong in the expensive direction: too low and the corpus report is noise
nobody reads, too low AND wired into injection and the agent silently
stops receiving guidance the ranking said it should have.

Neither corpus that ships in this repository contains an intentional
duplicate:

  * `commontrace/fixtures/fields/*.json` — eight unrelated fields (legal,
    coding, HR, robotics, sales, marketing, finance, clinical), authored
    per field, with no pair meant to restate another.
  * `commons/seed/substrate-v1.jsonl` — independently authored entries with
    no shared authorship with the fixtures.

So every pair either corpus reports at a given threshold is a FALSE
POSITIVE. Both corpora turn out to be silent at every threshold in the
sweep, which makes the pair COUNT the wrong statistic to calibrate on --
it is zero everywhere and would justify any threshold at all. The number
that actually bounds the default is the **highest similarity two
genuinely distinct lessons reach**: 0.167 here, across 2,163 pairs. A
threshold below that is measurably wrong; one just above it has no margin
for a corpus denser than these.

That is a ceiling on the default, not a proof that the default catches
real duplicates -- which is why the recall side is reported too, against
synthetic restatements built by perturbing real lessons (dropping a
sentence, reordering clauses, dropping a fifth of the words), where the
answer IS known.

Usage:
    python -m commontrace.reference.measure_redundancy
    python -m commontrace.reference.measure_redundancy --json
    python -m commontrace.reference.measure_redundancy --min-margin 1.5
"""
import argparse
import glob
import json
import os
import random
import statistics
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from commontrace import redundancy  # noqa: E402

SCHEMA_VERSION = "1.0.0"

DEFAULT_FIXTURES = os.path.join(_REPO, "commontrace", "fixtures", "fields")
DEFAULT_SEED = os.path.join(_REPO, "commons", "seed", "substrate-v1.jsonl")

SWEEP = (0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80)


def load_fixture_lessons(fixtures_dir):
    """(label, comparable text) for every lesson in the field fixtures."""
    items = []
    for path in sorted(glob.glob(os.path.join(fixtures_dir, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            field = json.load(fh)
        for lesson in field.get("lessons", []):
            label = f"{field.get('field', 'unknown')}/{lesson.get('name', '?')}"
            items.append((label, redundancy.comparable_text(lesson)))
    return items


def load_seed_entries(seed_path):
    """(label, comparable text) for every commons seed entry.

    A seed entry is a Trace-shaped row, not a Lesson, so its fields are
    mapped onto the ones `comparable_text` reads: the title is the
    description, the context is when it applies, the solution is the rule.
    That mapping is what `commontrace import` does for the same rows, so
    this measures the corpus as the product would actually store it.
    """
    items = []
    if not os.path.isfile(seed_path):
        return items
    with open(seed_path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            fm = {
                "description": row.get("title", ""),
                "applies_when": row.get("context_text", ""),
                "do_not_apply_when": "",
            }
            items.append((
                f"seed/{line_no}",
                redundancy.comparable_text(fm, row.get("solution_text", "")),
            ))
    return items


def _restate(text, rng):
    """A synthetic restatement: the same rule, written differently.

    Three perturbations, each of which a second author re-deriving the same
    lesson from a different trace cluster plausibly produces:
      - drop one sentence (they left out a caveat)
      - reorder the remaining sentences (they led with the condition)
      - drop a fifth of the words (they were terser)

    This is deliberately crude. It bounds recall against restatements that
    share vocabulary, which is the only kind a lexical measure can claim
    to catch -- see redundancy.py's module docstring on what it misses.
    """
    sentences = [s.strip() for s in text.replace("\n", ". ").split(".") if s.strip()]
    if len(sentences) > 2:
        sentences.pop(rng.randrange(len(sentences)))
    rng.shuffle(sentences)
    words = ". ".join(sentences).split()
    keep = [w for w in words if rng.random() > 0.2]
    return " ".join(keep) if keep else text


def distribution(items):
    """Similarity of every pair in a corpus with no intentional duplicates.

    This is the calibration instrument. `max` is the number the default
    threshold has to clear: it is the loudest a genuinely distinct pair
    gets, so a threshold below it flags real, non-redundant lessons as
    restatements of each other. p99 and mean are reported to show how far
    out in the tail that maximum sits -- a max close to the mean would
    mean the corpus has no discriminating power and the measurement is
    worthless.
    """
    tokens = [(label, redundancy.token_set(text)) for label, text in items]
    scores = []
    loudest = []
    for i in range(len(tokens)):
        for j in range(i + 1, len(tokens)):
            score = redundancy.jaccard(tokens[i][1], tokens[j][1])
            scores.append(score)
            loudest.append((score, tokens[i][0], tokens[j][0]))
    if not scores:
        return {"pairs": 0, "max": 0.0, "p99": 0.0, "mean": 0.0, "loudest": []}
    loudest.sort(reverse=True)
    return {
        "pairs": len(scores),
        "max": round(max(scores), 3),
        "p99": round(statistics.quantiles(scores, n=100)[98], 3) if len(scores) >= 100 else round(max(scores), 3),
        "mean": round(statistics.mean(scores), 3),
        "loudest": [
            {"a": a, "b": b, "similarity": round(score, 3)}
            for score, a, b in loudest[:5]
        ],
    }


def by_field(items):
    """Per-field distributions -- the adversarial case for a lexical measure.

    Lessons in one domain share that domain's vocabulary, so if
    `redundancy.COMPARED_FIELDS` were letting grouping metadata in, the
    same-field maximum would sit well above the cross-field one. Reported
    so that claim is checked rather than asserted.
    """
    grouped = {}
    for label, text in items:
        grouped.setdefault(label.split("/", 1)[0], []).append((label, text))
    return {
        field: distribution(group)
        for field, group in sorted(grouped.items())
        if len(group) > 1
    }


def measure(items, thresholds):
    """False positives per threshold on a corpus with no known duplicates."""
    out = {}
    for threshold in thresholds:
        pairs = redundancy.find_near_duplicates(items, threshold=threshold)
        out[f"{threshold:.2f}"] = {
            "pairs": len(pairs),
            "examples": [
                {"a": p.a, "b": p.b, "similarity": round(p.similarity, 3)}
                for p in pairs[:5]
            ],
        }
    return out


def measure_recall(items, thresholds, *, seed=0xC0FFEE):
    """Detection rate against synthetic restatements of the same corpus.

    Each lesson is paired with one perturbed copy of itself and nothing
    else, so at every threshold the question is simply "was the copy
    recognised". Reported alongside the false-positive sweep because the
    two move in opposite directions and a default chosen from one alone is
    chosen from half the evidence.
    """
    rng = random.Random(seed)
    restated = [(f"{label}~restated", _restate(text, rng)) for label, text in items]
    out = {}
    for threshold in thresholds:
        found = 0
        for (label, text), (_, copy) in zip(items, restated):
            match = redundancy.closest(copy, [(label, text)], threshold=threshold)
            if match is not None:
                found += 1
        out[f"{threshold:.2f}"] = {
            "detected": found,
            "of": len(items),
            "rate": round(found / len(items), 3) if items else 0.0,
        }
    return out


def render(report):
    dist = report["distribution"]
    worst_field, worst = max(
        dist["by_field"].items(), key=lambda kv: kv[1]["max"], default=("n/a", {"max": 0.0}),
    )
    observed = max(
        dist["fixtures"]["max"],
        dist["seed"].get("max", 0.0) if dist["seed"] else 0.0,
    )
    margin = (redundancy.DEFAULT_THRESHOLD / observed) if observed else float("inf")
    lines = [
        "# Near-duplicate threshold calibration",
        "",
        f"Default in `commontrace/redundancy.py`: **{redundancy.DEFAULT_THRESHOLD:.2f}** "
        f"({margin:.1f}x the loudest genuinely distinct pair)",
        "",
        "## What distinct lessons actually score",
        "",
        "The ceiling the default has to clear. Neither corpus contains a pair",
        "written to restate another, so the maximum here is noise, not signal.",
        "",
        "| corpus | pairs | max | p99 | mean |",
        "|---|---|---|---|---|",
        f"| field fixtures ({report['n_fixtures']} lessons, 8 fields) | "
        f"{dist['fixtures']['pairs']:,} | {dist['fixtures']['max']:.3f} | "
        f"{dist['fixtures']['p99']:.3f} | {dist['fixtures']['mean']:.3f} |",
    ]
    if dist["seed"]:
        lines.append(
            f"| commons seed ({report['n_seed']} entries) | {dist['seed']['pairs']:,} | "
            f"{dist['seed']['max']:.3f} | {dist['seed']['p99']:.3f} | {dist['seed']['mean']:.3f} |"
        )
    lines.append(
        f"| worst single field ({worst_field}) | {worst.get('pairs', 0):,} | "
        f"{worst.get('max', 0.0):.3f} | {worst.get('p99', 0.0):.3f} | "
        f"{worst.get('mean', 0.0):.3f} |"
    )
    lines.extend([
        "",
        "Loudest distinct pairs:",
        "",
    ])
    for example in dist["fixtures"]["loudest"][:3]:
        lines.append(f"- `{example['a']}` ~ `{example['b']}` ({example['similarity']})")
    lines.extend([
        "",
        "## Threshold sweep",
        "",
        "| threshold | field fixtures | commons seed | restatement recall (fixtures) |",
        "|---|---|---|---|",
    ])
    for threshold in report["thresholds"]:
        key = f"{threshold:.2f}"
        fixtures = report["fixtures"][key]["pairs"]
        seed = report["seed"][key]["pairs"] if report["seed"] else "n/a"
        recall = report["recall"][key]["rate"]
        lines.append(f"| {key} | {fixtures} | {seed} | {recall:.0%} |")
    lines.extend([
        "",
        f"Corpus sizes: {report['n_fixtures']} fixture lessons, "
        f"{report['n_seed']} commons seed entries.",
        "",
        "A pair reported on either corpus is a false positive by construction:",
        "neither contains a lesson written to restate another. Restatement recall",
        "is measured against synthetic perturbations of the fixture lessons, where",
        "the true answer is known -- see this script's docstring for what that",
        "does and does not bound.",
    ])
    top = report["fixtures"][f"{redundancy.DEFAULT_THRESHOLD:.2f}"]["examples"]
    if top:
        lines.extend(["", "## Flagged at the default threshold", ""])
        for example in top:
            lines.append(f"- `{example['a']}` ~ `{example['b']}` ({example['similarity']})")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixtures", default=DEFAULT_FIXTURES)
    parser.add_argument("--seed-corpus", default=DEFAULT_SEED)
    parser.add_argument("--json", action="store_true", help="raw JSON instead of markdown")
    parser.add_argument(
        "--max-false-positives", type=int, default=None,
        help="non-zero exit if the default threshold flags more than this many "
             "pairs on either corpus -- the CI gate on the default staying honest.",
    )
    parser.add_argument(
        "--min-margin", type=float, default=None,
        help="non-zero exit unless the default threshold is at least this "
             "multiple of the loudest genuinely distinct pair in either "
             "corpus. This is the gate that matters: the pair COUNT is zero "
             "at every threshold in the sweep and so cannot bound anything.",
    )
    args = parser.parse_args(argv)

    fixtures = load_fixture_lessons(args.fixtures)
    seed_items = load_seed_entries(args.seed_corpus)
    report = {
        "schema_version": SCHEMA_VERSION,
        "default_threshold": redundancy.DEFAULT_THRESHOLD,
        "thresholds": list(SWEEP),
        "n_fixtures": len(fixtures),
        "n_seed": len(seed_items),
        "fixtures": measure(fixtures, SWEEP),
        "seed": measure(seed_items, SWEEP) if seed_items else {},
        "recall": measure_recall(fixtures, SWEEP),
        "distribution": {
            "fixtures": distribution(fixtures),
            "seed": distribution(seed_items) if seed_items else {},
            "by_field": by_field(fixtures),
        },
    }

    print(json.dumps(report, indent=2) if args.json else render(report))

    if args.max_false_positives is not None:
        key = f"{redundancy.DEFAULT_THRESHOLD:.2f}"
        worst = max(
            report["fixtures"][key]["pairs"],
            report["seed"][key]["pairs"] if report["seed"] else 0,
        )
        if worst > args.max_false_positives:
            print(
                f"\nFAIL: default threshold {key} flags {worst} pair(s) on a corpus "
                f"with no intentional duplicates (max {args.max_false_positives}).",
                file=sys.stderr,
            )
            return 1

    if args.min_margin is not None:
        observed = max(
            report["distribution"]["fixtures"]["max"],
            report["distribution"]["seed"].get("max", 0.0)
            if report["distribution"]["seed"] else 0.0,
        )
        margin = (redundancy.DEFAULT_THRESHOLD / observed) if observed else float("inf")
        if margin < args.min_margin:
            print(
                f"\nFAIL: default threshold {redundancy.DEFAULT_THRESHOLD:.2f} is only "
                f"{margin:.2f}x the loudest genuinely distinct pair ({observed:.3f}); "
                f"{args.min_margin:.2f}x required.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
