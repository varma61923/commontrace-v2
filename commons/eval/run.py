"""Measure what the shipped commons corpus actually covers, on held-out probes.

WHY THIS EXISTS. `commons_overlap` returns a coverage percentage, and that
percentage is the number a prospect decides on. A number nobody has
validated is worse than no number: it would be quoted, and it would be
wrong. So the corpus that ships (commons/seed/substrate-v1.jsonl) is
evaluated against a probe set it was not built from
(commons/eval/probes-v1.jsonl), and whatever comes out is what gets
reported -- including when that is unflattering.

WHAT IS HELD OUT, AND WHAT IS NOT. The probes are written symptom-first, in
the vocabulary an on-call engineer or an agent would use in the moment
("customer charged twice for one order"), not in the corpus's own
vocabulary ("payment webhook delivered more than once"). No probe shares
its phrasing with the record it targets. But the same author wrote both
files, and that is a real limitation that no amount of paraphrasing
removes: shared conceptual framing survives paraphrase. So read the recall
here as an OPTIMISTIC bound on a real fleet, not an estimate of one. The
number that would settle it is a measurement against failures a customer
collected, and this repository does not have one.

The negative controls carry less of that caveat and so are the more
trustworthy half: 22 real substrate failures deliberately absent from the
corpus, several chosen as near misses (leap-year date arithmetic against a
corpus that contains DST; replica staleness against a corpus full of
database entries). A matcher that "covers" those is reporting coverage it
does not have, and no amount of author bias can flatter that number.

NOTHING HERE TUNES ANYTHING. The headline is computed at the shipped
default threshold. The sensitivity table is printed because a single
operating point hides how sharp the cliff is, not as an invitation to pick
a threshold that reads better; the shipped default is not changed on the
strength of this file.

Run:  python commons/eval/run.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hub import commons  # noqa: E402

CORPUS = ROOT / "commons" / "seed" / "substrate-v1.jsonl"
PROBES = Path(__file__).resolve().parent / "probes-v1.jsonl"


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def evaluate(threshold: float | None = None) -> dict:
    """Return the measured result at `threshold` (default: the shipped one).

    Signing on both sides goes through `commons.signature_for`, the same
    call the Hub makes when seeding and the same one the client makes when
    a fleet asks -- an evaluation that signed differently from production
    would measure something nobody ever runs.
    """
    threshold = commons.DEFAULT_COMMONS_THRESHOLD if threshold is None else threshold
    corpus = _load(CORPUS)
    probes = _load(PROBES)

    corpus_titles = [r["title"] for r in corpus]
    corpus_sigs = [
        commons.signature_for(r["title"], r.get("context_text", ""), r.get("tags") or [])
        for r in corpus
    ]
    submitted = [
        (p["label"], commons.signature_for(p["label"], p.get("text", ""), p.get("tags") or []))
        for p in probes
    ]

    results = []
    for probe, (idx, sim) in zip(probes, commons.best_matches(submitted, corpus_sigs)):
        matched = sim >= threshold and idx >= 0
        results.append({
            "label": probe["label"],
            "expect": probe["expect"],
            "target": probe.get("target"),
            "matched": matched,
            "matched_title": corpus_titles[idx] if idx >= 0 else None,
            "similarity": round(sim, 4),
        })

    pos = [r for r in results if r["expect"] == "covered"]
    neg = [r for r in results if r["expect"] == "uncovered"]
    hits = [r for r in pos if r["matched"]]
    right = [r for r in hits if r["matched_title"] == r["target"]]
    false_pos = [r for r in neg if r["matched"]]

    return {
        "threshold": threshold,
        "n_corpus": len(corpus),
        "n_positive": len(pos),
        "n_negative": len(neg),
        "recall": len(hits) / len(pos) if pos else 0.0,
        "right_row_rate": len(right) / len(hits) if hits else 0.0,
        "false_positive_rate": len(false_pos) / len(neg) if neg else 0.0,
        "results": results,
    }


def main() -> int:
    r = evaluate()
    print(f"corpus:    {r['n_corpus']} records (commons/seed/substrate-v1.jsonl)")
    print(f"probes:    {r['n_positive']} held-out positives, {r['n_negative']} negative controls")
    print(f"threshold: {r['threshold']} (the shipped default, unmodified)")
    print()
    print(f"recall on positives:      {r['recall']:.1%}  "
          f"({sum(1 for x in r['results'] if x['expect'] == 'covered' and x['matched'])}"
          f"/{r['n_positive']} paraphrased failures matched)")
    print(f"  of those, right record: {r['right_row_rate']:.1%}  "
          "(matched the record the probe was written against)")
    print(f"false positives on controls: {r['false_positive_rate']:.1%}  "
          f"({sum(1 for x in r['results'] if x['expect'] == 'uncovered' and x['matched'])}"
          f"/{r['n_negative']} absent failures wrongly reported as covered)")
    print()

    print("sensitivity (informational -- the shipped threshold is not chosen from this):")
    print("  threshold   recall   false-positive")
    for t in (0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5):
        s = evaluate(t)
        mark = "  <- shipped" if abs(t - commons.DEFAULT_COMMONS_THRESHOLD) < 1e-9 else ""
        print(f"  {t:<11.2f} {s['recall']:>6.1%}   {s['false_positive_rate']:>12.1%}{mark}")
    print()

    missed = [x for x in r["results"] if x["expect"] == "covered" and not x["matched"]]
    if missed:
        print(f"missed positives ({len(missed)}) -- the corpus has the knowledge, "
              "the matcher did not find it:")
        for x in sorted(missed, key=lambda x: -x["similarity"])[:10]:
            print(f"  {x['similarity']:.3f}  {x['label']}")
        if len(missed) > 10:
            print(f"  ... and {len(missed) - 10} more")
        print()

    wrong = [x for x in r["results"]
             if x["expect"] == "uncovered" and x["matched"]]
    if wrong:
        print("false positives -- reported as covered but the corpus does not contain it:")
        for x in wrong:
            print(f"  {x['similarity']:.3f}  {x['label']}  ->  {x['matched_title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
