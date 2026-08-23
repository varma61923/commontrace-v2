"""Why the two retrieval tiers get wildly different recall on identical data.

STRATEGY.md §12.4.3 asserted that per-org retrieval "runs on the same
lexical machinery" as the commons matcher and is therefore capped by the
same 10.9% recall defect. That claim was made by analogy and never
measured. This script measures it, on the same corpus and the same probes,
and it is wrong.

The two tiers share a tokenizer and almost nothing else that matters:

  commons  (hub/commons.py -> commontrace/overlap.py)
      MinHash -> Jaccard -> compare against a THRESHOLD -> covered / not.
      Emits a percentage that gets quoted to a customer.

  per-org  (commontrace/retrieval.py:rank_lessons)
      Weighted token overlap -> sort -> return top-k. NO threshold; any
      single shared content word puts a lesson on the list.
      Emits a ranked list a human or agent skims.

Different output contracts demand opposite trades, and each tier made the
right one for what it emits. Run: python commons/eval/retrieval_tiers.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from commontrace import retrieval  # noqa: E402

CORPUS = ROOT / "commons" / "seed" / "substrate-v1.jsonl"
PROBES = Path(__file__).resolve().parent / "probes-v1.jsonl"


def _load(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def as_lessons(corpus: list[dict]) -> list[tuple[str, dict]]:
    """Seed records in the local tier's own lesson shape. `applies_when` is
    the activation condition, which rank_lessons weights above description."""
    return [
        (r["title"], {
            "name": r["title"],
            "description": r["title"],
            "applies_when": r.get("context_text", ""),
            "tags": r.get("tags") or [],
            "domain": "",
        })
        for r in corpus
    ]


def _query(p: dict) -> str:
    return f"{p['label']} {p.get('text', '')}"


def evaluate() -> dict:
    corpus, probes = _load(CORPUS), _load(PROBES)
    lessons = as_lessons(corpus)
    pos = [p for p in probes if p["expect"] == "covered"]
    neg = [p for p in probes if p["expect"] == "uncovered"]

    def recall_at(k: int) -> float:
        hits = sum(
            1 for p in pos
            if any(r.slug == p["target"] for r in retrieval.rank_lessons(_query(p), lessons, top_k=k))
        )
        return hits / len(pos) if pos else 0.0

    def top_score(p: dict) -> float:
        r = retrieval.rank_lessons(_query(p), lessons, top_k=1)
        return r[0].score if r else 0.0

    return {
        "n_corpus": len(corpus),
        "n_pos": len(pos),
        "n_neg": len(neg),
        "recall": {k: recall_at(k) for k in (1, 3, 5, 10)},
        "recall_anywhere": recall_at(len(lessons)),
        # The cost of having no threshold: what fraction of failures the
        # corpus CANNOT answer still come back with a non-empty result.
        "neg_returns_something": {
            k: (
                sum(1 for p in neg if retrieval.rank_lessons(_query(p), lessons, top_k=k)) / len(neg)
                if neg else 0.0
            )
            for k in (1, 3, 5)
        },
        "score_true": [top_score(p) for p in pos],
        "score_absent": [top_score(p) for p in neg],
    }


def main() -> int:
    r = evaluate()
    print(f"corpus {r['n_corpus']} records | {r['n_pos']} held-out positives, "
          f"{r['n_neg']} negative controls")
    print()
    print("PER-ORG lexical retrieval (commontrace/retrieval.py, ranked, no threshold)")
    for k, v in r["recall"].items():
        print(f"  recall@{k:<3}                  {v:>6.1%}")
    print(f"  present anywhere in ranking  {r['recall_anywhere']:>6.1%}")
    print()
    print("  Cost of no threshold -- absent failures that still return a result:")
    for k, v in r["neg_returns_something"].items():
        print(f"    top_k={k}                     {v:>6.1%}")
    print()
    t, a = r["score_true"], r["score_absent"]
    if t:
        print(f"  top-1 score, true match      median {statistics.median(t):.1f}  "
              f"range {min(t):.1f}-{max(t):.1f}")
    else:
        print("  top-1 score, true match      (no positive probes)")
    if a:
        print(f"  top-1 score, absent          median {statistics.median(a):.1f}  "
              f"range {min(a):.1f}-{max(a):.1f}")
    else:
        print("  top-1 score, absent          (no negative-control probes)")
    print("  (distributions overlap -- the score separates on average, not per case)")
    print()
    print("COMMONS matcher, same corpus and probes (commons/eval/RESULTS.md)")
    print("  recall at the shipped threshold  10.9%")
    print("  false positives on the controls   0.0%")
    print()
    print("The difference is the THRESHOLD, not the tokenizer. Each tier made the")
    print("right trade for what it emits: a quoted percentage needs precision, a")
    print("skimmed ranked list needs recall. See commons/eval/RESULTS.md 'Two tiers'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
