"""LoCoMo JSONL adapter: load (query, expected_doc_ids) pairs + score retrieval.

Expected JSONL shape per line (aliases accepted defensively):
  {"question"|"query"|"input"|"prompt": str,
   "expected_doc_ids"|"expected"|"doc_ids"|"relevant_doc_ids"|"answer_docs": [str]}
"""
from __future__ import annotations

import json
import os
from typing import Any

DATASET = "locomo"

_QUERY_KEYS = ("question", "query", "input", "prompt", "text")
_EXPECTED_KEYS = (
    "expected_doc_ids", "expected", "doc_ids", "relevant_doc_ids",
    "answer_docs", "gold_doc_ids", "target_docs",
)


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    try:
        return [str(v) for v in value]
    except TypeError:
        return [str(value)]


def load_corpus(path: str) -> list[tuple[str, list[str]]]:
    """Load a LoCoMo-style JSONL corpus into (query, expected_doc_ids) pairs."""
    pairs: list[tuple[str, list[str]]] = []
    if not path or not os.path.exists(path):
        return pairs
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            query = ""
            for key in _QUERY_KEYS:
                if obj.get(key):
                    query = str(obj[key])
                    break
            expected: list[str] = []
            for key in _EXPECTED_KEYS:
                if obj.get(key) is not None:
                    expected = _str_list(obj[key])
                    break
            if query.strip() and expected:
                pairs.append((query.strip(), expected))
    return pairs


def run(
    corpus_path: str,
    lessons: list[tuple[str, dict]],
    top_k: int = 5,
    **rank_kwargs: Any,
) -> dict[str, Any]:
    """Score retrieval.rank_lessons over the corpus: P@1 + recall@k.

    Defensive: rank errors per query count as misses, never raise.
    """
    from commontrace import retrieval

    pairs = load_corpus(corpus_path)
    n = len(pairs)
    if n == 0:
        return {"dataset": DATASET, "n": 0, "p_at_1": 0.0, "recall": 0.0, "top_k": top_k}
    hits_1 = 0
    hits_k = 0
    for query, expected in pairs:
        try:
            ranked = retrieval.rank_lessons(query, lessons, top_k=top_k, **rank_kwargs)
            got = [r.slug or r.path for r in ranked]
        except Exception:
            got = []
        want = set(expected)
        if got and got[0] in want:
            hits_1 += 1
        if any(g in want for g in got):
            hits_k += 1
    return {
        "dataset": DATASET,
        "n": n,
        "p_at_1": round(hits_1 / n, 4),
        "recall": round(hits_k / n, 4),
        "top_k": top_k,
    }
