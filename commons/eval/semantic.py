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

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

THRESHOLDS = (0.80, 0.75, 0.70, 0.65, 0.60, 0.55, 0.50, 0.45, 0.40)


def _load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _probe_text(p: dict) -> str:
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
