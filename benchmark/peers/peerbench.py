#!/usr/bin/env python3
"""Peer retrieval benchmark: CommonTrace's retrievers against the retrieval
stacks other agent-memory products ship, on the public datasets those
products publish results on (LoCoMo, LongMemEval). Results and method:
benchmark/peers/README.md.

Scored on RETRIEVAL, from the datasets' own evidence labels:
  * LoCoMo       -- each question lists the dialogue turns that answer it.
                    Unit = turn. Adversarial (category 5, no answer) excluded.
  * LongMemEval  -- each question lists its answer sessions. Documents are
                    turns; a session's rank is its best turn's rank (the
                    paper's session-level retrieval metric). Abstention
                    questions (no answer session) excluded.
No LLM, no judge: recall@k, NDCG@10 and MRR are computed from the labels,
so every number here is reproducible on a laptop.

Every system runs at its shipped defaults.

    python benchmark/peers/peerbench.py --download
    python benchmark/peers/peerbench.py --dataset locomo --systems ct-lexical,bm25,chroma

Peer libraries are optional (`pip install rank-bm25 chromadb "mem0ai[nlp,extras]"
sentence-transformers`); a system whose library is missing is skipped and
named, never silently dropped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import time
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

DATASETS = {
    # The LoCoMo release (Maharana et al., ACL 2024), as published by its authors.
    "locomo10.json": "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json",
    # LongMemEval (Wu et al., ICLR 2025), the "S" haystack variant.
    "longmemeval_s.json": "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_s",
}


def download(data_dir: str) -> None:
    import urllib.request
    os.makedirs(data_dir, exist_ok=True)
    for name, url in DATASETS.items():
        target = os.path.join(data_dir, name)
        if os.path.exists(target):
            print(f"have {target}")
            continue
        print(f"fetching {url}")
        urllib.request.urlretrieve(url, target)


# --- datasets -----------------------------------------------------------------

@dataclass
class Doc:
    id: str
    text: str
    unit: str          # what relevance is judged on (turn id, or session id)


@dataclass
class Query:
    id: str
    text: str
    relevant: set[str]
    category: str


@dataclass
class Case:
    corpus_id: str
    docs: list[Doc]
    queries: list[Query] = field(default_factory=list)


LOCOMO_CATEGORIES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop"}


def load_locomo(path: str) -> list[Case]:
    cases = []
    for conv in json.load(open(path)):
        c = conv["conversation"]
        docs = []
        n = 1
        while f"session_{n}" in c:
            date = c.get(f"session_{n}_date_time", "")
            for turn in c[f"session_{n}"]:
                text = f"{turn['speaker']}: {turn['text']}"
                if turn.get("blip_caption"):
                    text += f" [shares a photo: {turn['blip_caption']}]"
                if date:
                    text = f"({date}) {text}"
                docs.append(Doc(turn["dia_id"], text, turn["dia_id"]))
            n += 1
        case = Case(conv["sample_id"], docs)
        valid = {d.id for d in docs}
        for i, qa in enumerate(conv["qa"]):
            cat = qa.get("category")
            if cat == 5 or cat not in LOCOMO_CATEGORIES:
                continue
            rel = {e.strip() for e in qa.get("evidence", []) if e.strip() in valid}
            if not rel:
                continue
            case.queries.append(Query(f"{conv['sample_id']}-{i}", qa["question"], rel,
                                      LOCOMO_CATEGORIES[cat]))
        cases.append(case)
    return cases


def load_longmemeval(path: str, per_type: int, seed: int = 0, max_chars: int = 2000) -> list[Case]:
    data = json.load(open(path))
    data = [q for q in data if not q["question_id"].endswith("_abs") and q["answer_session_ids"]]
    by_type: dict[str, list] = {}
    for q in data:
        by_type.setdefault(q["question_type"], []).append(q)
    rng = random.Random(seed)
    chosen = []
    for qtype in sorted(by_type):
        pool = sorted(by_type[qtype], key=lambda q: q["question_id"])
        chosen += pool if per_type <= 0 else rng.sample(pool, min(per_type, len(pool)))
    cases = []
    for q in chosen:
        docs = []
        for sid, date, session in zip(q["haystack_session_ids"], q["haystack_dates"],
                                      q["haystack_sessions"]):
            for j, turn in enumerate(session):
                text = f"({date}) {turn['role']}: {turn['content']}"[:max_chars]
                docs.append(Doc(f"{sid}#{j}", text, sid))
        case = Case(q["question_id"], docs)
        case.queries.append(Query(q["question_id"], q["question"],
                                  set(q["answer_session_ids"]), q["question_type"]))
        cases.append(case)
    return cases


# --- metrics --------------------------------------------------------------------

def unit_ranking(ranked_doc_ids: list[str], unit_of: dict[str, str]) -> list[str]:
    seen, out = set(), []
    for d in ranked_doc_ids:
        u = unit_of[d]
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def metrics(units: list[str], relevant: set[str]) -> dict[str, float]:
    out = {}
    for k in (1, 5, 10, 20):
        out[f"recall@{k}"] = len(set(units[:k]) & relevant) / len(relevant)
    dcg = sum(1.0 / math.log2(i + 2) for i, u in enumerate(units[:10]) if u in relevant)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(10, len(relevant))))
    out["ndcg@10"] = dcg / idcg if idcg else 0.0
    rr = 0.0
    for i, u in enumerate(units):
        if u in relevant:
            rr = 1.0 / (i + 1)
            break
    out["mrr"] = rr
    return out


# --- embeddings (shared, cached) ------------------------------------------------

_MODELS: dict = {}


def encoder(name: str):
    if name not in _MODELS:
        from sentence_transformers import SentenceTransformer
        _MODELS[name] = SentenceTransformer(name, device="cpu")
    return _MODELS[name]


def embed(name: str, texts: list[str], cache_dir: str):
    """Unit-normalized embeddings, cached on disk by content hash."""
    import numpy as np
    os.makedirs(cache_dir, exist_ok=True)
    h = hashlib.sha256(("\x1f".join(texts) + "\x1e" + name).encode()).hexdigest()[:24]
    path = os.path.join(cache_dir, f"{name.replace('/', '_')}-{h}.npy")
    if os.path.exists(path):
        return np.load(path)
    vecs = encoder(name).encode(texts, batch_size=64, normalize_embeddings=True,
                                convert_to_numpy=True, show_progress_bar=False)
    np.save(path, vecs)
    return vecs


# --- systems --------------------------------------------------------------------

class System:
    name = "?"
    note = ""

    def index(self, docs: list[Doc]) -> None:
        raise NotImplementedError

    def search(self, query: str, k: int) -> list[str]:
        raise NotImplementedError


class CTLexical(System):
    """CommonTrace's lexical retriever exactly as `retrieve`/`query` run it:
    each memory is a lesson whose description is the memory text."""

    def __init__(self, scorer: str | None = None, floor: float | None = None, label: str = ""):
        from commontrace import retrieval
        self.retrieval = retrieval
        self.scorer = scorer or retrieval.SCORER_IDF
        self.floor = floor
        self.name = label or f"commontrace lexical ({self.scorer})"

    def index(self, docs):
        from commontrace import lesson_cache
        self.lessons = [(d.id, {"name": d.id, "description": d.text, "status": "active"})
                        for d in docs]
        # What the product passes: commontrace/lesson_cache.py's TermCache --
        # each lesson's tokenized fields plus its file stamp, which lets
        # rank_lessons build its corpus index once and reuse it. Built here,
        # so its cost is reported as indexing, not charged to every query.
        tc = lesson_cache.TermCache({path: lesson_cache.field_terms(fm) for path, fm in self.lessons})
        tc.stamps = {path: (0, i) for i, (path, _fm) in enumerate(self.lessons)}
        tc.lessons = self.lessons
        tc.fingerprint = tuple((path, tc.stamps[path]) for path, _fm in self.lessons)
        tc.fingerprint_hash = hash(tc.fingerprint)
        self.term_cache = tc
        self.retrieval._corpus_index(self.lessons, tc, self.scorer)

    def search(self, query, k):
        ranked = self.retrieval.rank_lessons(query, self.lessons, top_k=k, floor=self.floor,
                                             scorer=self.scorer, term_cache=self.term_cache)
        return [r.slug for r in ranked]


class Dense(System):
    """Exact cosine search over unit-normalized sentence embeddings."""

    def __init__(self, model: str, label: str, cache_dir: str, note: str = ""):
        self.model, self.name, self.cache_dir, self.note = model, label, cache_dir, note

    def index(self, docs):
        self.ids = [d.id for d in docs]
        self.vecs = embed(self.model, [d.text for d in docs], self.cache_dir)

    def search(self, query, k):
        import numpy as np
        q = encoder(self.model).encode([query], normalize_embeddings=True,
                                       convert_to_numpy=True, show_progress_bar=False)[0]
        scores = self.vecs @ q
        top = np.argsort(-scores)[:k]
        return [self.ids[i] for i in top]


_TOKEN = re.compile(r"\w+", re.UNICODE)


class BM25(System):
    """Okapi BM25 (rank_bm25, k1=1.5, b=0.75): the keyword arm of the hybrid
    search Graphiti/Zep, Hindsight and mem0 describe."""

    name = "BM25 (Okapi)"

    def index(self, docs):
        from rank_bm25 import BM25Okapi
        self.ids = [d.id for d in docs]
        self.bm = BM25Okapi([_TOKEN.findall(d.text.lower()) for d in docs])

    def search(self, query, k):
        import numpy as np
        scores = self.bm.get_scores(_TOKEN.findall(query.lower()))
        top = np.argsort(-scores)[:k]
        return [self.ids[i] for i in top if scores[i] > 0]


class RRF(System):
    """Rank fusion as `--fusion rrf` runs it: each arm is asked for the same
    number of results the caller asked for, and the two lists are fused by
    position (RRF, k=60) -- the product fuses `top_k` from each arm, so this
    does too, rather than a deeper pool the product never sees."""

    def __init__(self, arms: list[System], label: str, k: int = 60):
        self.arms, self.name, self.k = arms, label, k

    def index(self, docs):
        for a in self.arms:
            a.index(docs)

    def search(self, query, k):
        from commontrace import retrieval
        fused = retrieval.reciprocal_rank_fusion(
            {a.name: a.search(query, k) for a in self.arms}, k=self.k, top_k=k)
        return [slug for slug, _ in fused]


class Reranked(System):
    """A first stage reordered by commontrace's reranker, as `--rerank
    cross-encoder` runs it: the first stage hands over
    `rerank_arm.pool_size(k)` candidates and `rerank_arm.rerank` keeps k.
    Each memory's text is what the cross-encoder reads."""

    def __init__(self, first: System, label: str):
        from commontrace import rerank_arm
        self.first, self.name, self.rerank_arm = first, label, rerank_arm

    def index(self, docs):
        self.first.index(docs)
        self.text = {d.id: d.text for d in docs}

    def search(self, query, k):
        pool = self.first.search(query, self.rerank_arm.pool_size(k))
        page, _ = self.rerank_arm.rerank(query, pool, self.text, k)
        return [slug for slug, _ in page]


class Chroma(System):
    """chromadb, in-process, default embedding function (ONNX all-MiniLM-L6-v2, HNSW)."""

    name = "Chroma (default embedding)"

    def __init__(self):
        import chromadb
        self.client = chromadb.EphemeralClient()
        self.n = 0

    def index(self, docs):
        self.n += 1
        self.col = self.client.create_collection(f"case-{self.n}-{os.getpid()}",
                                                 metadata={"hnsw:space": "cosine"})
        for i in range(0, len(docs), 2000):
            batch = docs[i:i + 2000]
            self.col.add(ids=[d.id for d in batch], documents=[d.text for d in batch])

    def search(self, query, k):
        return self.col.query(query_texts=[query], n_results=min(k, self.col.count()))["ids"][0]


class Mem0(System):
    """mem0 OSS 2.x, `infer=False` (memories stored verbatim, no LLM), with its
    hybrid search on: vector (all-MiniLM-L6-v2) + lemmatized BM25 + entity
    boosts, local Qdrant. Default search threshold (0.1)."""

    name = "mem0 2.x (vector+BM25+entities, infer=False)"

    def __init__(self, workdir: str, rerank: bool = False):
        os.environ.setdefault("OPENAI_API_KEY", "sk-unused-no-llm-calls")
        os.environ["MEM0_TELEMETRY"] = "False"
        self.workdir = workdir
        self.n = 0
        # mem0's own reranker, with the same cross-encoder commontrace's
        # uses. mem0 reorders the results it would return (`limit`); it does
        # not fetch a deeper pool, so this is measured as it ships.
        self.rerank = rerank
        if rerank:
            self.name = "mem0 2.x + its cross-encoder rerank (same model)"

    def index(self, docs):
        from mem0 import Memory
        self.n += 1
        cfg = {
            "embedder": {"provider": "huggingface",
                         "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"}},
            "vector_store": {"provider": "qdrant", "config": {
                "collection_name": f"case{self.n}", "embedding_model_dims": 384,
                "path": os.path.join(self.workdir, f"qd{os.getpid()}-{self.n}"), "on_disk": False}},
        }
        if self.rerank:
            cfg["reranker"] = {"provider": "sentence_transformer", "config": {
                "model": "cross-encoder/ms-marco-MiniLM-L-6-v2", "device": "cpu"}}
        self.m = Memory.from_config(cfg)
        self.user = f"u{self.n}"
        self.by_text: dict[str, str] = {}
        for d in docs:
            self.m.add(d.text, user_id=self.user, infer=False, metadata={"doc": d.id})

    def search(self, query, k):
        res = self.m.search(query, filters={"user_id": self.user}, top_k=k,
                            rerank=self.rerank)["results"]
        return [r["metadata"]["doc"] for r in res if r.get("metadata", {}).get("doc")]


# --- runner ---------------------------------------------------------------------

def run(system: System, cases: list[Case], k: int = 20) -> dict:
    per_query, idx_times, q_times, n_docs = [], [], [], 0
    for case in cases:
        unit_of = {d.id: d.unit for d in case.docs}
        t = time.perf_counter()
        system.index(case.docs)
        idx_times.append(time.perf_counter() - t)
        n_docs += len(case.docs)
        for q in case.queries:
            t = time.perf_counter()
            ranked = system.search(q.text, k)
            q_times.append(time.perf_counter() - t)
            m = metrics(unit_ranking(ranked, unit_of), q.relevant)
            m["category"] = q.category
            per_query.append(m)
    keys = [k for k in per_query[0] if k != "category"]
    overall = {k: statistics.fmean(m[k] for m in per_query) for k in keys}
    cats = sorted({m["category"] for m in per_query})
    by_cat = {c: {k: statistics.fmean(m[k] for m in per_query if m["category"] == c) for k in keys}
              for c in cats}
    q_ms = sorted(t * 1000 for t in q_times)
    return {
        "system": system.name, "note": system.note,
        "n_queries": len(per_query), "n_docs": n_docs,
        "overall": overall, "by_category": by_cat,
        "query_ms_p50": q_ms[len(q_ms) // 2], "query_ms_p95": q_ms[int(len(q_ms) * 0.95)],
        "index_ms_per_doc": 1000 * sum(idx_times) / max(1, n_docs),
        "per_query": per_query,
    }


def build(names: list[str], cache: str, workdir: str) -> list[System]:
    out = []
    for n in names:
        try:
            system = _build_one(n, cache, workdir)
        except ImportError as exc:
            print(f"SKIPPED {n}: {exc} (install it to include this system)", file=sys.stderr)
            continue
        out.append(system)
    return out


def _build_one(n: str, cache: str, workdir: str) -> System:
    mpnet = "multi-qa-mpnet-base-dot-v1"  # the model commontrace index builds with
    if n == "ct-lexical":
        return CTLexical()
    if n.startswith("ct-lexical:"):
        return CTLexical(scorer=n.split(":", 1)[1])
    if n == "ct-semantic":
        return Dense(mpnet, "commontrace semantic (mpnet)", cache)
    if n == "ct-fusion":
        return RRF([CTLexical(), Dense(mpnet, "commontrace semantic (mpnet)", cache)],
                   "commontrace fusion (lexical+semantic, RRF)")
    if n == "ct-fusion-v3":
        return RRF([CTLexical(scorer="idf-v3"), Dense(mpnet, "commontrace semantic (mpnet)", cache)],
                   "commontrace fusion (idf-v3+semantic, RRF)")
    if n in ("ct-fusion-rerank", "ct-fusion-v3-rerank", "ct-lexical-rerank"):
        first = {
            "ct-fusion-rerank": lambda: _build_one("ct-fusion", cache, workdir),
            "ct-fusion-v3-rerank": lambda: _build_one("ct-fusion-v3", cache, workdir),
            "ct-lexical-rerank": CTLexical,
        }[n]()
        return Reranked(first, f"{first.name} + cross-encoder rerank")
    if n == "bm25":
        return BM25()
    if n == "dense-minilm":
        return Dense("all-MiniLM-L6-v2", "dense all-MiniLM-L6-v2 (exact cosine)", cache)
    if n == "hybrid":
        return RRF([BM25(), Dense("all-MiniLM-L6-v2", "dense MiniLM", cache)],
                   "hybrid BM25+MiniLM (RRF)")
    if n == "chroma":
        return Chroma()
    if n == "mem0":
        return Mem0(workdir)
    if n == "mem0-rerank":
        return Mem0(workdir, rerank=True)
    raise SystemExit(f"unknown system {n}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=["locomo", "longmemeval"])
    ap.add_argument("--download", action="store_true", help="Fetch the public datasets.")
    ap.add_argument("--data-dir", default=os.path.join(HERE, "data"))
    ap.add_argument("--systems", default="ct-lexical,bm25")
    ap.add_argument("--per-type", type=int, default=10)
    ap.add_argument("--limit-cases", type=int, default=0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    if args.download:
        download(args.data_dir)
        if not args.dataset:
            return
    if not args.dataset:
        ap.error("--dataset is required")
    cache = os.path.join(HERE, "emb_cache")
    workdir = os.path.join(HERE, "work")
    os.makedirs(workdir, exist_ok=True)
    if args.dataset == "locomo":
        cases = load_locomo(os.path.join(args.data_dir, "locomo10.json"))
    else:
        cases = load_longmemeval(os.path.join(args.data_dir, "longmemeval_s.json"), args.per_type)
    if args.limit_cases:
        cases = cases[:args.limit_cases]
    results = []
    for system in build(args.systems.split(","), cache, workdir):
        t = time.time()
        r = run(system, cases)
        r["wall_s"] = round(time.time() - t, 1)
        results.append(r)
        o = r["overall"]
        print(f"{r['system']:52s} R@5 {o['recall@5']:.3f}  R@10 {o['recall@10']:.3f}  "
              f"NDCG@10 {o['ndcg@10']:.3f}  MRR {o['mrr']:.3f}  "
              f"q p50 {r['query_ms_p50']:.1f}ms p95 {r['query_ms_p95']:.1f}ms  "
              f"idx {r['index_ms_per_doc']:.2f}ms/doc  ({r['wall_s']}s)", flush=True)
    if args.out:
        for r in results:
            r.pop("per_query", None)
        json.dump({"dataset": args.dataset, "n_cases": len(cases), "results": results},
                  open(args.out, "w"), indent=2)


if __name__ == "__main__":
    main()
