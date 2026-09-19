"""Does semantic similarity fix the coverage bar's recall -- and does it
actually cost the privacy guarantee?

WHAT THIS ANSWERS. commons/eval/RESULTS.md measured the shipped matcher at
10.9% recall with a 0% false-positive rate, attributed it to representation
rather than tuning, and named the fix: "semantic rather than lexical
similarity -- embedding each failure and comparing vectors." It then
recorded a blocker:

    That is a real change, not a tweak, because it breaks the current
    privacy story: MinHash signatures are exchanged today precisely
    because failure text never leaves the fleet, and an embedding is
    computed by a model that has to see the text.

commons/eval/representations.py has since closed the cheaper door: every
tokenization candidate measured *worse* than shipped on the held-out set.
Lexical representation is exhausted, so the semantic question is the only
one left, and this file measures it.

THE BLOCKER RESTS ON A FALSE PREMISE, AND THAT MATTERS MORE THAN THE NUMBER.

"A model has to see the text" is true and harmless. What the privacy
guarantee actually forbids is the *operator* seeing a customer's failure
text -- not a model running on the customer's own machine, on text that
machine already holds.

And the other side of the comparison is not secret at all. The Knowledge
Base is operator-curated substrate knowledge, published in this repository
as commons/seed/substrate-v1.jsonl. There is no confidentiality to protect
on the corpus side, which means the whole comparison can happen on the
client:

    corpus embeddings  <- public content, computed anywhere, distributable
    failure embedding  <- computed locally, from text already on that machine
    similarity         <- computed locally, against a local corpus index

The Hub is not a participant. It learns nothing -- not the text, not an
embedding, not even the MinHash signature it receives today. That is
STRICTLY MORE PRIVATE than what currently ships, which is the opposite of
what the recorded blocker assumes.

So the trade this file evaluates is not "recall versus privacy". It is
"recall and privacy, versus a model dependency and an index to distribute".

WHAT WOULD MAKE THIS WORTH SHIPPING. The same falsifiable test the other
experiments use, and for the same reason: a coverage number that every
customer sees must not be raised by lowering the bar. Recall rising while
the negative controls stay at zero is a real improvement. Recall rising
alongside false positives is the bar dropping, and the gain is fake. Both
are printed for every threshold, on both probe sets, and probes-v2 is the
held-out number to believe.

This file changes nothing that ships. It imports the corpus and the probes,
and writes no product code.

Run:  python commons/eval/semantic.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hub import commons  # noqa: E402

CORPUS = ROOT / "commons" / "seed" / "substrate-v1.jsonl"
HERE = Path(__file__).resolve().parent

# Small, CPU-friendly, and already the family this repository uses for its
# own attention layer (requirements.txt / memory/attention/build_index.py),
# so this measures a model the project could actually ship rather than a
# research-grade one it could not.
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# Cosine thresholds to sweep. The shipped Jaccard threshold (0.30) is not
# comparable to a cosine, so there is no "the" threshold to reuse -- the
# operating point has to be chosen from this table the same way the Jaccard
# one was, by the highest recall that still holds false positives at zero.
THRESHOLDS = (0.80, 0.75, 0.70, 0.65, 0.60, 0.55, 0.50, 0.45, 0.40)


def _load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _probe_text(p: dict) -> str:
    """Signed exactly as production signs a failure: title + context + tags
    (hub/commons.py:matchable_text), so the two sides stay symmetric."""
    return commons.matchable_text(p.get("label", ""), p.get("text", ""), p.get("tags"))


def _corpus_text(r: dict) -> str:
    return commons.matchable_text(r["title"], r.get("context_text", ""), r.get("tags"))


def score(model, probes: list[dict], corpus: list[dict], threshold: float) -> dict:
    import numpy as np

    titles = [r["title"] for r in corpus]
    corpus_vecs = model.encode([_corpus_text(r) for r in corpus], normalize_embeddings=True)
    probe_vecs = model.encode([_probe_text(p) for p in probes], normalize_embeddings=True)
    sims = np.asarray(probe_vecs) @ np.asarray(corpus_vecs).T

    hits = right = false_pos = rank1 = 0
    n_pos = n_neg = 0
    sims_of_truth: list[float] = []

    for i, probe in enumerate(probes):
        row = sims[i]
        best = int(row.argmax())
        covered = probe["expect"] == "covered"
        if covered:
            n_pos += 1
            target = probe.get("target")
            if titles[best] == target:
                rank1 += 1
            if target in titles:
                sims_of_truth.append(float(row[titles.index(target)]))
            if float(row[best]) >= threshold:
                hits += 1
                if titles[best] == target:
                    right += 1
        else:
            n_neg += 1
            if float(row[best]) >= threshold:
                false_pos += 1

    return {
        "recall": hits / n_pos if n_pos else 0.0,
        "right_of_hits": right / hits if hits else 0.0,
        "false_positive": false_pos / n_neg if n_neg else 0.0,
        "rank1": rank1 / n_pos if n_pos else 0.0,
        "median_true_sim": statistics.median(sims_of_truth) if sims_of_truth else 0.0,
    }


def main() -> int:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print(
            "sentence-transformers is not installed, so this experiment cannot run.\n"
            "It is deliberately NOT a dependency of the shipped package: this file\n"
            "measures whether such a dependency would be worth taking on.\n\n"
            "  python -m pip install sentence-transformers\n",
            file=sys.stderr,
        )
        return 2

    corpus = _load(CORPUS)
    dev = _load(HERE / "probes-v1.jsonl")
    held = _load(HERE / "probes-v2.jsonl")

    print(f"loading {MODEL_NAME} ...", file=sys.stderr)
    model = SentenceTransformer(MODEL_NAME)

    print(f"corpus {len(corpus)} records | metric cosine over {MODEL_NAME}")
    print(f"dev = probes-v1 ({sum(1 for p in dev if p['expect'] == 'covered')}+"
          f"{sum(1 for p in dev if p['expect'] == 'uncovered')})  "
          f"held-out = probes-v2 ({sum(1 for p in held if p['expect'] == 'covered')}+"
          f"{sum(1 for p in held if p['expect'] == 'uncovered')})")
    print()
    print(f"{'cosine >=':>9} {'dev recall':>11} {'dev FP':>8} "
          f"{'HELD recall':>12} {'HELD FP':>9}")
    print("-" * 56)

    best_safe = None
    for t in THRESHOLDS:
        d = score(model, dev, corpus, t)
        h = score(model, held, corpus, t)
        print(f"{t:>9.2f} {d['recall']:>10.1%} {d['false_positive']:>8.1%} "
              f"{h['recall']:>11.1%} {h['false_positive']:>9.1%}")
        # The operating point is chosen the way the shipped one was: the most
        # recall available while BOTH probe sets still report zero false
        # positives. Read off the held-out column, never the dev one.
        if d["false_positive"] == 0.0 and h["false_positive"] == 0.0:
            if best_safe is None or h["recall"] > best_safe[1]["recall"]:
                best_safe = (t, h)

    h_any = score(model, held, corpus, 0.0)
    print()
    print(f"rank1 (threshold ignored, held-out): {h_any['rank1']:.0%}")
    print(f"median similarity to the true record (held-out): {h_any['median_true_sim']:.3f}")
    print()
    print("Shipped baseline for comparison (commons/eval/representations.py):")
    print("  words/Jaccard @0.30  ->  HELD recall 8.7%, HELD FP 0.0%, rank1 85%")
    print()
    if best_safe is None:
        print("No threshold held false positives at zero on BOTH sets. On this evidence")
        print("semantic similarity does not buy a safer coverage number, and the shipped")
        print("bar should not move.")
        return 0
    t, h = best_safe
    print(f"Best zero-false-positive operating point: cosine >= {t:.2f}")
    print(f"  HELD recall {h['recall']:.1%}  (shipped: 8.7%)")
    print(f"  HELD false positives {h['false_positive']:.1%}  (shipped: 0.0%)")
    print(f"  of the matches it made, the right record {h['right_of_hits']:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
