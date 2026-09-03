"""Can the commons be SEARCHED, and what does each way of searching cost?

THE QUESTION
------------
The commons ships exactly one query surface: `commons_overlap`, which
MinHashes a fleet's failures locally, compares each against the corpus at a
THRESHOLD, and emits a coverage percentage. commons/eval/RESULTS.md
measures that at 10.9% recall -- it misses roughly nine of every ten
failures the corpus provably contains.

STRATEGY.md §12.7 established that the threshold, not the matcher, is what
discards those nine: the same corpus and the same tokenizer, ranked and
returned top-k with no threshold, finds the right record 84.8% of the time
at k=1. Its conclusion was that the open question is the OUTPUT CONTRACT --
should the commons return ranked candidates alongside (never instead of)
the coverage figure?

That measurement used commontrace/retrieval.py, which reads the query TEXT.
The commons cannot: its entire privacy proposition is that failure text
never leaves the fleet, only MinHash signatures do. So §12.7's number does
not transfer to the commons for free, and the question this script exists
to answer is the one nobody had measured:

    Ranked top-k over MINHASH SIGNATURES -- no threshold, no text leaving
    the fleet -- what recall does that get?

If it is good, the commons gets a Stack-Overflow-shaped search surface at
zero privacy cost, and §11.1's conclusion that fixing recall requires
embeddings (and therefore requires retracting the privacy guarantee) is
wrong about this surface.

WHAT IS COMPARED
----------------
Three ways of asking the same corpus the same 46 held-out questions:

  1. threshold  -- what ships today. Jaccard >= 0.30 -> covered/not.
  2. sig_rank   -- rank by estimated Jaccard, return top-k, NO threshold.
                   Signature-in, so the privacy guarantee is unchanged.
  3. text_rank  -- commontrace/retrieval.py over the query text. The
                   §12.7 number. Requires sending text; included as the
                   ceiling that a privacy-preserving mode is measured
                   against, not as a recommendation.

Negative controls are reported for every mode, because a ranked surface
with no threshold returns SOMETHING for every query by construction. That
is acceptable for candidates a human judges and is never acceptable for a
coverage figure, which is why this script reports both and why the shipped
coverage number is not touched by anything measured here.

Run: python commons/eval/search_modes.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from commontrace import overlap, retrieval  # noqa: E402
from hub import commons as hub_commons  # noqa: E402

CORPUS = ROOT / "commons" / "seed" / "substrate-v1.jsonl"
PROBES = Path(__file__).resolve().parent / "probes-v1.jsonl"

KS = (1, 3, 5, 10)


def _load(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _query_text(p: dict) -> str:
    return f"{p['label']} {p.get('text', '')}"


def _probe_signature(p: dict) -> list[int]:
    """Signed exactly as a client signs a recurring failure, so what is
    measured here is what the wire actually carries."""
    return hub_commons.signature_for(p["label"], p.get("text", ""), p.get("tags") or [])


def _corpus_signatures(corpus: list[dict]) -> list[tuple[str, list[int]]]:
    return [
        (r["title"], hub_commons.signature_for(r["title"], r.get("context_text", ""), r.get("tags") or []))
        for r in corpus
    ]


def _as_lessons(corpus: list[dict]) -> list[tuple[str, dict]]:
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


def sig_rank(probe: dict, corpus_sigs: list[tuple[str, list[int]]], top_k: int) -> list[tuple[str, float]]:
    """Rank the corpus by estimated Jaccard against the probe's signature.

    The ONLY difference from what ships today is the absence of a
    threshold: same signatures, same estimator, same corpus.
    """
    sig = _probe_signature(probe)
    scored = [(title, overlap.estimate_jaccard(sig, s)) for title, s in corpus_sigs]
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


def evaluate() -> dict:
    corpus, probes = _load(CORPUS), _load(PROBES)
    pos = [p for p in probes if p["expect"] == "covered"]
    neg = [p for p in probes if p["expect"] == "uncovered"]
    corpus_sigs = _corpus_signatures(corpus)
    lessons = _as_lessons(corpus)

    threshold = hub_commons.DEFAULT_COMMONS_THRESHOLD

    def threshold_recall() -> float:
        hits = 0
        for p in pos:
            best = sig_rank(p, corpus_sigs, 1)
            if best and best[0][1] >= threshold and best[0][0] == p["target"]:
                hits += 1
        return hits / len(pos) if pos else 0.0

    def threshold_false_positive() -> float:
        fp = 0
        for p in neg:
            best = sig_rank(p, corpus_sigs, 1)
            if best and best[0][1] >= threshold:
                fp += 1
        return fp / len(neg) if neg else 0.0

    def sig_recall_at(k: int) -> float:
        hits = sum(
            1 for p in pos
            if any(title == p["target"] for title, _ in sig_rank(p, corpus_sigs, k))
        )
        return hits / len(pos) if pos else 0.0

    def text_recall_at(k: int) -> float:
        hits = sum(
            1 for p in pos
            if any(r.slug == p["target"] for r in retrieval.rank_lessons(_query_text(p), lessons, top_k=k))
        )
        return hits / len(pos) if pos else 0.0

    # A signature-ranked result is only useful if a NON-ZERO score comes
    # back; estimate_jaccard returns 0.0 when no permutation agrees, and a
    # list of zero-scored records is noise wearing a ranking's clothes.
    def sig_nonzero_at(k: int, probes_: list[dict]) -> float:
        n = sum(1 for p in probes_ if any(s > 0 for _, s in sig_rank(p, corpus_sigs, k)))
        return n / len(probes_) if probes_ else 0.0

    def sig_top_score(p: dict) -> float:
        r = sig_rank(p, corpus_sigs, 1)
        return r[0][1] if r else 0.0

    return {
        "n_corpus": len(corpus),
        "n_pos": len(pos),
        "n_neg": len(neg),
        "threshold": threshold,
        "threshold_recall": threshold_recall(),
        "threshold_false_positive": threshold_false_positive(),
        "sig_recall": {k: sig_recall_at(k) for k in KS},
        "sig_recall_anywhere": sig_recall_at(len(corpus)),
        "text_recall": {k: text_recall_at(k) for k in KS},
        "sig_nonzero_pos": {k: sig_nonzero_at(k, pos) for k in KS},
        "sig_nonzero_neg": {k: sig_nonzero_at(k, neg) for k in KS},
        "sig_score_true": [sig_top_score(p) for p in pos],
        "sig_score_absent": [sig_top_score(p) for p in neg],
    }


def _fmt_range(vals: list[float]) -> str:
    if not vals:
        return "(none)"
    return f"median {statistics.median(vals):.3f}  range {min(vals):.3f}-{max(vals):.3f}"


def main() -> int:
    r = evaluate()
    print(f"corpus {r['n_corpus']} records | {r['n_pos']} held-out positives, "
          f"{r['n_neg']} negative controls")
    print()
    print(f"1. THRESHOLD (what ships: coverage %, cutoff {r['threshold']})")
    print(f"   recall on positives          {r['threshold_recall']:>7.1%}")
    print(f"   false positives on controls  {r['threshold_false_positive']:>7.1%}")
    print()
    print("2. SIGNATURE RANKING (no threshold, no text leaves the fleet)")
    for k in KS:
        print(f"   recall@{k:<3}                  {r['sig_recall'][k]:>7.1%}")
    print(f"   present anywhere in ranking  {r['sig_recall_anywhere']:>7.1%}")
    print()
    print("   Non-zero score returned (a zero-scored list is not an answer):")
    for k in KS:
        print(f"     positives, top_k={k:<3}       {r['sig_nonzero_pos'][k]:>7.1%}")
    for k in KS:
        print(f"     ABSENT,    top_k={k:<3}       {r['sig_nonzero_neg'][k]:>7.1%}")
    print()
    print(f"   top-1 score, true match      {_fmt_range(r['sig_score_true'])}")
    print(f"   top-1 score, absent          {_fmt_range(r['sig_score_absent'])}")
    print()
    print("3. TEXT RANKING (§12.7's number -- requires sending text; the ceiling)")
    for k in KS:
        print(f"   recall@{k:<3}                  {r['text_recall'][k]:>7.1%}")
    print()
    best_sig = max(r["sig_recall"].values())
    print(f"Signature ranking recovers {best_sig:.1%} at its best k, against "
          f"{r['threshold_recall']:.1%} for the shipped threshold")
    print(f"and {max(r['text_recall'].values()):.1%} for text ranking, which the commons cannot use")
    print("without retracting 'failure text never leaves your fleet'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
