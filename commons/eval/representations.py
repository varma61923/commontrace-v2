"""Would a different token representation fix the commons' recall problem?"""
from __future__ import annotations

import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from commontrace.overlap import (  # noqa: E402
    _MERSENNE_61,
    _STOPWORDS,
    _permutations,
    _stable_hash,
    estimate_jaccard,
)
from hub import commons  # noqa: E402

NUM_PERM = commons.COMMONS_NUM_PERM
THRESHOLD = commons.DEFAULT_COMMONS_THRESHOLD

_WORD = re.compile(r"\w+", re.UNICODE)


def words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOPWORDS and len(w) > 1}


def _normalized(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def _char_ngrams(text: str, n: int) -> set[str]:
    s = _normalized(text)
    return {s[i : i + n] for i in range(len(s) - n + 1)} if len(s) >= n else set()


def char4(text: str) -> set[str]:
    return _char_ngrams(text, 4)


def char5(text: str) -> set[str]:
    return _char_ngrams(text, 5)


_SUFFIXES = ("ing", "ies", "ed", "es", "s", "ly", "tion", "sion", "ment")


def _stem(w: str) -> str:
    for suf in _SUFFIXES:
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def stems(text: str) -> set[str]:
    return {_stem(w) for w in words(text)}


def words_and_char4(text: str) -> set[str]:
    return words(text) | {"#" + g for g in char4(text)}


CANDIDATES = {
    "words (shipped)": words,
    "stems": stems,
    "char4": char4,
    "char5": char5,
    "words+char4": words_and_char4,
}


def signature(tokens: set[str]) -> list[int]:
    if not tokens:
        return [0] * NUM_PERM
    hashes = [_stable_hash(t) for t in tokens]
    return [min((a * h + b) % _MERSENNE_61 for h in hashes) for a, b in _permutations(NUM_PERM)]


def score(tokenize, probes: list[dict], corpus: list[dict], threshold: float = THRESHOLD) -> dict:
    titles = [r["title"] for r in corpus]
    corpus_sigs = [
        signature(tokenize(commons.matchable_text(r["title"], r.get("context_text", ""), r.get("tags"))))
        for r in corpus
    ]

    hits = right = false_pos = 0
    rank1 = 0
    sims_of_truth = []
    n_pos = n_neg = 0
    for p in probes:
        text = " ".join([p["label"], p.get("text", ""), " ".join(p.get("tags") or [])])
        sig = signature(tokenize(text))
        best_i, best_sim = -1, 0.0
        for i, cand in enumerate(corpus_sigs):
            sim = estimate_jaccard(sig, cand)
            if sim > best_sim:
                best_i, best_sim = i, sim
        matched = best_sim >= threshold and best_i >= 0
        if p["expect"] == "covered":
            n_pos += 1
            if best_i >= 0 and titles[best_i] == p.get("target"):
                rank1 += 1
            tgt = p.get("target")
            if tgt in titles:
                sims_of_truth.append(estimate_jaccard(sig, corpus_sigs[titles.index(tgt)]))
            if matched:
                hits += 1
                if titles[best_i] == p.get("target"):
                    right += 1
        else:
            n_neg += 1
            if matched:
                false_pos += 1

    return {
        "rank1": rank1 / n_pos if n_pos else 0.0,
        "median_true_sim": statistics.median(sims_of_truth) if sims_of_truth else 0.0,
        "recall": hits / n_pos if n_pos else 0.0,
        "right_of_hits": right / hits if hits else 0.0,
        "false_positive": false_pos / n_neg if n_neg else 0.0,
        "hits": hits, "n_pos": n_pos, "false_pos": false_pos, "n_neg": n_neg,
    }
