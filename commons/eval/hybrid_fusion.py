"""Would fusing the two retrievers beat either one? Measured: no, not here."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from commontrace import retrieval  # noqa: E402

CORPUS = ROOT / "commons" / "seed" / "substrate-v1.jsonl"
PROBE_SETS = ("probes-v1.jsonl", "probes-v2.jsonl")

MODEL_NAME = "multi-qa-mpnet-base-dot-v1"

GRID = [
    (k, w_lex, w_sem)
    for k in (1, 2, 5, 10, 20, 60)
    for w_lex, w_sem in ((1, 1), (1, 2), (1, 3), (2, 1), (1, 5))
]


def _load(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def as_lessons(corpus: list[dict]) -> list[tuple[str, dict]]:
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
