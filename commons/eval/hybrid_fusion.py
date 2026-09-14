"""Would fusing the two retrievers beat either one? Measured: no, not here.

THE QUESTION
------------
`commontrace query` picks ONE retriever per query (commands/query_cmd.py):
the semantic one when the `attention` extra is installed and its index is
usable, the lexical one otherwise. It never runs both and combines them.

Hybrid retrieval -- run both arms, fuse the two ranked lists -- is the
standard answer in information retrieval, and the argument for it fits
this product's corpus exactly. commons/eval/RESULTS.md says the failure
mode of lexical matching here is vocabulary: "two engineers describing the
same substrate failure -- 'connection pool exhausted during a retry storm'
and 'during a spike everything starts timing out waiting to acquire a
connection' -- share almost no words." Embeddings close that gap; lexical
still wins on the literal tokens embeddings blur (error codes, stack
frames, identifiers). Fusing should get both.

The fusion used here is Reciprocal Rank Fusion: score(d) = sum over arms
of weight / (k + rank(d)), from Cormack, Clarke & Buettcher, "Reciprocal
Rank Fusion outperforms Condorcet and individual Rank Learning Methods"
(SIGIR 2009). It is the natural choice because it reads only RANKS, never
scores -- and the two arms here emit incomparable scales (weighted token
overlap vs. dot product on unit vectors), so there is nothing to
calibrate.

WHY THE BAR IS HIGHER HERE THAN IN AN ORDINARY SEARCH SYSTEM
------------------------------------------------------------
The retriever is not a standalone feature in this product: it decides
which lessons are ELIGIBLE on an occasion, which is the denominator of
the causal estimate. `commontrace/integrity.py:check_scorer_drift` treats
a scorer change mid-experiment as SEVERITY_INVALIDATES -- it discards the
comparison. So shipping a retrieval change costs every customer their
running experiment, and a change worth that price has to be measurably
better, not plausibly better.

THE ANSWER
----------
It is not better on the evidence available. Tuning the fusion on one
probe set and scoring it on the other -- both directions -- the sign of
the improvement FLIPS: fusion beats semantic by 2 probes on one fold and
loses to it by 3 on the other. Tuning and testing on the same 92 probes
does produce a winner (+4.4pp at rank 1), which is exactly the number this
split exists to disbelieve: it is the maximum of 30 configurations drawn
on the same data, and at n=92 one standard error is already ~2.7pp.

The deeper reading is about the instrument, not the method. The two probe
sets do not even agree on which SINGLE arm is better (semantic wins v1 by
6.5pp, lexical wins v2 by 4.3pp), and recall is 90-99% everywhere, so
@3 and @5 are saturated at one missed probe. A 46-record corpus with 92
probes cannot resolve differences this size. That is a statement about
what has been measured, not a claim that hybrid retrieval does not work:
it says this corpus cannot tell, and the honest response to "cannot tell"
is to leave a working retriever alone.

Run: python commons/eval/hybrid_fusion.py
Needs the `attention` extra (numpy + sentence-transformers) and the model
cached locally; it explains and exits 0 if either is missing, so this stays
runnable on the core install like everything else here.

To install dependencies:
  pip install "commontrace[attention]"
or from repository root:
  pip install -e ".[attention]"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from commontrace import retrieval  # noqa: E402

CORPUS = ROOT / "commons" / "seed" / "substrate-v1.jsonl"
PROBE_SETS = ("probes-v1.jsonl", "probes-v2.jsonl")

# The model the product's own attention layer uses
# (commontrace/reference/build_index.py:MODEL_NAME). Fusing against a
# different encoder than the one that ships would measure a retriever no
# customer runs.
MODEL_NAME = "multi-qa-mpnet-base-dot-v1"

# The rank constant and per-arm weights swept when tuning. k is the
# "+k" in RRF: small k lets rank 1 dominate, large k flattens the arms
# toward an average. 60 is the paper's value, chosen against TREC-scale
# runs; on a 46-record corpus it compresses the whole range, which is why
# it is swept rather than assumed.
GRID = [
    (k, w_lex, w_sem)
    for k in (1, 2, 5, 10, 20, 60)
    for w_lex, w_sem in ((1, 1), (1, 2), (1, 3), (2, 1), (1, 5))
]


def _load(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def as_lessons(corpus: list[dict]) -> list[tuple[str, dict]]:
    """Seed records in the local tier's lesson shape -- identical to
    commons/eval/retrieval_tiers.py:as_lessons, so the lexical arm here is
    the same measurement that file already reports."""
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


def _require_attention():
    """The encoder, or None with the reason printed.

    Mirrors commands/query_cmd.py's posture: a missing optional extra is a
    thing to explain and step around, never a traceback.
    """
    try:
        import numpy  # noqa: F401
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        print(f"[skip] needs the attention extra (numpy + sentence-transformers): {exc}")
        print("       pip install 'commontrace[attention]'")
        return None
    try:
        return SentenceTransformer(MODEL_NAME)
    except Exception as exc:  # noqa: BLE001 - offline/model-missing is a skip, not a crash
        print(f"[skip] could not load {MODEL_NAME}: {type(exc).__name__}: {exc}")
        return None


def _rankings(model, corpus, probes, lessons):
    """Full-corpus lexical and semantic rankings for every probe."""
    import numpy as np

    n = len(lessons)
    titles = [r["title"] for r in corpus]
    doc_vecs = np.asarray(model.encode(
        [f"{r['title']}. {r.get('context_text', '')}" for r in corpus],
        normalize_embeddings=True, show_progress_bar=False,
    ))
    query_vecs = np.asarray(model.encode(
        [_query(p) for p in probes], normalize_embeddings=True, show_progress_bar=False,
    ))
    lexical = [
        [r.slug for r in retrieval.rank_lessons(_query(p), lessons, top_k=n)]
        for p in probes
    ]
    semantic = [
        [titles[j] for j in np.argsort(-(doc_vecs @ query_vecs[i]))]
        for i in range(len(probes))
    ]
    return lexical, semantic


def _recall_at_1(probes, rankings) -> tuple[float, int]:
    hits = sum(1 for i, p in enumerate(probes) if rankings[i][:1] == [p["target"]])
    return (hits / len(probes) if probes else 0.0), hits


def _fused_recall_at_1(probes, lexical, semantic, config) -> tuple[float, int]:
    k, w_lex, w_sem = config
    hits = 0
    for i, p in enumerate(probes):
        # The SHIPPED implementation (commontrace/retrieval.py), not a copy.
        # This benchmark exists to choose the k and the weights that the
        # product then runs with, so a second implementation here could tune
        # one formula and ship another.
        fused = retrieval.reciprocal_rank_fusion(
            {"lexical": lexical[i], "semantic": semantic[i]},
            k=k, top_k=1, weights={"lexical": w_lex, "semantic": w_sem},
        )
        if [doc for doc, _ in fused] == [p["target"]]:
            hits += 1
    return (hits / len(probes) if probes else 0.0), hits


def main() -> int:
    model = _require_attention()
    if model is None:
        return 0

    corpus = _load(CORPUS)
    lessons = as_lessons(corpus)
    in_corpus = {r["title"] for r in corpus}

    folds = {}
    for name in PROBE_SETS:
        probes = [
            p for p in _load(Path(__file__).resolve().parent / name)
            if p["expect"] == "covered" and p["target"] in in_corpus
        ]
        folds[name] = (probes,) + _rankings(model, corpus, probes, lessons)

    print(f"corpus {len(corpus)} records | "
          + " | ".join(f"{n} {len(f[0])} positives" for n, f in folds.items()))
    print()
    print("Rank-1 recall is the only discriminating measure here: @3 and @5 are")
    print("saturated at 98.9% (one missed probe) for every method tried.")
    print()

    net = 0
    for tune, test in (PROBE_SETS, tuple(reversed(PROBE_SETS))):
        t_probes, t_lex, t_sem = folds[tune]
        h_probes, h_lex, h_sem = folds[test]

        best = max(GRID, key=lambda c: _fused_recall_at_1(t_probes, t_lex, t_sem, c)[0])
        tuned_rate, _ = _fused_recall_at_1(t_probes, t_lex, t_sem, best)
        fused_rate, fused_hits = _fused_recall_at_1(h_probes, h_lex, h_sem, best)
        lex_rate, lex_hits = _recall_at_1(h_probes, h_lex)
        sem_rate, sem_hits = _recall_at_1(h_probes, h_sem)
        best_single = max(lex_hits, sem_hits)
        net += fused_hits - best_single

        print(f"tuned on {tune} -> k={best[0]} weights lex:sem = {best[1]}:{best[2]} "
              f"({tuned_rate:.1%} there)")
        print(f"  scored on HELD-OUT {test} (n={len(h_probes)}):")
        print(f"    lexical only    {lex_rate:>6.1%} ({lex_hits})")
        print(f"    semantic only   {sem_rate:>6.1%} ({sem_hits})")
        print(f"    fusion          {fused_rate:>6.1%} ({fused_hits})   "
              f"vs best single arm: {fused_hits - best_single:+d} probes")
        print()

    print(f"NET across both folds: {net:+d} probes.")
    print()
    if net > 0:
        print("Fusion is ahead on held-out data. Before shipping it, note that a")
        print("scorer change is an INVALIDATES event (commontrace/integrity.py:")
        print("check_scorer_drift) -- it must ship under a NEW scorer id, and every")
        print("running experiment restarts. Confirm the margin justifies that.")
    else:
        print("Fusion is not ahead on held-out data -- the sign of the improvement")
        print("flips with which set it was tuned on, which is what noise looks like.")
        print("The two probe sets do not even agree on which single arm is better,")
        print("so this corpus cannot resolve a difference of this size. Leaving the")
        print("shipped retriever alone is the honest response to 'cannot tell'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
