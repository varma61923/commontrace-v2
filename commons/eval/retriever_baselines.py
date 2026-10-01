"""The shipped lexical ranker against the standard reference retrievers, on both labelled corpora.

Arms: the shipped ranker (commontrace/retrieval.py), textbook Okapi BM25 (k1=1.2, b=0.75), TF-IDF cosine,
and, when `sentence-transformers` is installed, dense retrieval with the same local model the `attention`
extra uses (commontrace/semantic.py) plus a stronger small one, each alone and fused with the shipped ranker
by Reciprocal Rank Fusion. All arms see the same lesson text: name, description, applies_when and tags.

Two corpora, reported separately because they answer different questions:
- commontrace/fixtures/fields/ is the corpus the shipped scorer was tuned on, so it can only show a regression.
- commons/ (seed + probes-v1) was written independently with paraphrased queries: the fair comparison.

Run: python commons/eval/retriever_baselines.py [--no-dense]
Results and their limits are in commons/eval/RESULTS.md.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import retrieval_tiers  # noqa: E402

from commontrace import retrieval  # noqa: E402

DENSE_MODELS = ("sentence-transformers/all-MiniLM-L6-v2", "BAAI/bge-small-en-v1.5")
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an the of to in on for and or is are was were be been it its this that with as at by from not no do does "
    "did when than then but if into out up our we you they their there".split())


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def _doc_text(fm: dict) -> str:
    fields = " ".join(str(fm.get(k, "")) for k in ("name", "description", "applies_when"))
    return fields + " " + " ".join(fm.get("tags") or [])


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.2, b: float = 0.75):
        self.docs = [Counter(_tokens(d)) for d in docs]
        self.lengths = [sum(c.values()) for c in self.docs]
        self.avg = sum(self.lengths) / len(self.lengths)
        df = Counter(t for c in self.docs for t in c)
        self.idf = {t: math.log(1 + (len(docs) - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query: str) -> list[float]:
        terms = _tokens(query)
        out = []
        for counts, length in zip(self.docs, self.lengths):
            norm = self.k1 * (1 - self.b + self.b * length / self.avg)
            out.append(sum(self.idf[t] * counts[t] * (self.k1 + 1) / (counts[t] + norm) for t in terms if t in counts))
        return out


class TFIDF:
    def __init__(self, docs: list[str]):
        counts = [Counter(_tokens(d)) for d in docs]
        df = Counter(t for c in counts for t in c)
        self.idf = {t: math.log((1 + len(docs)) / (1 + f)) + 1 for t, f in df.items()}
        self.vecs = [self._unit({t: f * self.idf[t] for t, f in c.items()}) for c in counts]

    @staticmethod
    def _unit(vec: dict) -> dict:
        norm = math.sqrt(sum(x * x for x in vec.values())) or 1.0
        return {t: x / norm for t, x in vec.items()}

    def scores(self, query: str) -> list[float]:
        q = self._unit({t: f * self.idf.get(t, 0.0) for t, f in Counter(_tokens(query)).items()})
        return [sum(w * d.get(t, 0.0) for t, w in q.items()) for d in self.vecs]


class Dense:
    def __init__(self, docs: list[str], model: str):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model)
        self.prefix = "Represent this sentence for searching relevant passages: " if "bge" in model else ""
        self.matrix = self.model.encode(docs, normalize_embeddings=True)

    def scores(self, query: str) -> list[float]:
        return (self.matrix @ self.model.encode([self.prefix + query], normalize_embeddings=True)[0]).tolist()


def _ranked(names: list[str], scores: list[float], matched_only: bool = False) -> list[str]:
    """Best first. `matched_only` drops documents sharing no term with the query, as the shipped ranker does."""
    order = sorted(range(len(names)), key=lambda i: -scores[i])
    return [names[i] for i in order if not matched_only or scores[i] > 0]


def _rrf(*lists: list[str], k: int = 60) -> list[str]:
    fused: Counter = Counter()
    for ranked in lists:
        for rank, name in enumerate(ranked, 1):
            fused[name] += 1.0 / (k + rank)
    return [name for name, _ in fused.most_common()]


def _corpora():
    for path in sorted(glob.glob(os.path.join(ROOT, "commontrace", "fixtures", "fields", "*.json"))):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        lessons = [(f"<fixture>/{item['name']}.md", dict(item)) for item in doc["lessons"]]
        yield "fields", lessons, [(c["query"], set(c["relevant"])) for c in doc["queries"]]
    lessons = retrieval_tiers.as_lessons(retrieval_tiers._load(retrieval_tiers.CORPUS))
    probes = retrieval_tiers._load(retrieval_tiers.PROBES)
    yield "commons", lessons, [(retrieval_tiers._query(p), {p["target"]}) for p in probes if p["expect"] == "covered"]


def evaluate(dense_models=DENSE_MODELS) -> dict:
    totals: dict = {}
    for corpus, lessons, cases in _corpora():
        names = [os.path.splitext(os.path.basename(p))[0] if p.startswith("<fixture>") else p for p, _ in lessons]
        texts = [_doc_text(fm) for _, fm in lessons]
        bm25, tfidf = BM25(texts), TFIDF(texts)

        def shipped(q, lessons=lessons):
            return [r.slug for r in retrieval.rank_lessons(q, lessons, top_k=len(lessons))]
        arms = {"shipped lexical": shipped,
                "Okapi BM25": lambda q, m=bm25, n=names: _ranked(n, m.scores(q), matched_only=True),
                "TF-IDF cosine": lambda q, m=tfidf, n=names: _ranked(n, m.scores(q), matched_only=True)}
        for model in dense_models:
            short = model.split("/")[-1]
            enc = Dense(texts, model)
            arms[f"dense {short}"] = lambda q, m=enc, n=names: _ranked(n, m.scores(q))
            arms[f"RRF shipped + {short}"] = lambda q, m=enc, n=names, s=shipped: _rrf(s(q), _ranked(n, m.scores(q)))
        for arm, rank in arms.items():
            row = totals.setdefault(corpus, {}).setdefault(arm, {"n": 0, "r1": 0, "r3": 0, "mrr": 0.0, "ms": []})
            for query, relevant in cases:
                start = time.perf_counter()
                ranked = rank(query)
                row["ms"].append((time.perf_counter() - start) * 1000)
                row["n"] += 1
                row["r1"] += bool(ranked[:1] and ranked[0] in relevant)
                row["r3"] += bool(relevant & set(ranked[:3]))
                row["mrr"] += next((1 / i for i, name in enumerate(ranked, 1) if name in relevant), 0.0)
    return {corpus: {arm: {"queries": r["n"], "recall@1": r["r1"] / r["n"], "recall@3": r["r3"] / r["n"],
                           "mrr": r["mrr"] / r["n"], "p50_ms": statistics.median(r["ms"])}
                     for arm, r in arms.items()}
            for corpus, arms in totals.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-dense", action="store_true", help="skip the embedding arms")
    args = ap.parse_args()
    models: tuple = ()
    if not args.no_dense:
        try:
            import sentence_transformers  # noqa: F401
            models = DENSE_MODELS
        except ImportError:
            print("sentence-transformers is not installed: dense arms skipped", file=sys.stderr)
    for corpus, arms in evaluate(models).items():
        n = next(iter(arms.values()))["queries"]
        print(f"\n{corpus} ({n} queries)\n| retriever | recall@1 | recall@3 | MRR | p50 ms |")
        print("|---|---:|---:|---:|---:|")
        for arm, r in arms.items():
            print(f"| {arm} | {r['recall@1']:.1%} | {r['recall@3']:.1%} | {r['mrr']:.3f} | {r['p50_ms']:.2f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
