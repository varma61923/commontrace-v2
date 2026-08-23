"""Would a different token representation fix the commons' recall problem?

WHY THIS IS AN EXPERIMENT AND NOT A PATCH. commons/eval/RESULTS.md measured
the shipped matcher at 10.9% recall with a 0% false-positive rate: when a
fleet describes a failure in its own words, the commons finds knowledge it
provably holds about one time in nine. That is the single defect blocking
the product's core proposition, and it is a *representation* problem --
Jaccard over content words is lexical, and two engineers describing the
same failure share almost no words.

The obvious move is to change how text is tokenized. This file measures
what that would buy, and changes nothing that ships. commontrace/overlap.py
is imported, never modified; every candidate below reuses that module's
permutations, its hash, and its `estimate_jaccard` unaltered, and every
candidate is scored at the shipped 0.30 threshold. The only thing that
varies between rows of the output is which token set goes into the MinHash.

WHY THE DECISION IS NOT MINE TO MAKE. A better representation raises the
coverage number every customer sees, which is exactly the shape of change
that must not be made to make numbers look better. So the honest question
is not "did recall go up" but "did it go up because the matcher found real
matches, or because the bar dropped". There is a falsifiable test for that
and it is already built: the negative controls. Recall rising while false
positives stay at zero means the product genuinely got better. Recall
rising alongside false positives means the bar dropped, and the gain is
fake. Both numbers are printed side by side for every candidate, and the
right-record rate with them -- a match that lands on the wrong trace is
worse than no match, because it spends the customer's trust.

TWO SETS, AND WHY. probes-v1 is now a DEV set: its per-probe misses were
read while thinking about this problem, so any candidate that looks good on
it may simply be fitted to it. probes-v2 was written before any candidate
here existed -- 46 second phrasings of the same corpus records in a terse,
log-line voice, plus 19 fresh absent failures -- and is the number to
believe. Both are printed, because a candidate that wins on dev and loses
on held-out is the most important result this file can produce.

Run:  python commons/eval/representations.py
"""
from __future__ import annotations

import json
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

CORPUS = ROOT / "commons" / "seed" / "substrate-v1.jsonl"
HERE = Path(__file__).resolve().parent
NUM_PERM = commons.COMMONS_NUM_PERM
THRESHOLD = commons.DEFAULT_COMMONS_THRESHOLD

_WORD = re.compile(r"[a-z0-9]+")


# --- Candidate token representations ------------------------------------
#
# Each takes the SAME text that production signs (commons.matchable_text on
# the corpus side, label + text + tags on the probe side) and returns a
# token set. Nothing else differs between candidates.


def words(text: str) -> set[str]:
    """The shipped representation: lowercase word tokens, stopwords out,
    single characters out. Reproduced here rather than imported so the
    comparison is visibly like-for-like."""
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOPWORDS and len(w) > 1}


def _normalized(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def _char_ngrams(text: str, n: int) -> set[str]:
    s = _normalized(text)
    return {s[i : i + n] for i in range(len(s) - n + 1)} if len(s) >= n else set()


def char4(text: str) -> set[str]:
    """Character 4-grams. The motivation is morphological: 'exhausted' and
    'exhaustion', 'retry' and 'retries', 'timing out' and 'timeout' share no
    word token at all but share most of their character runs."""
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
    """Crude suffix stripping -- the cheap half of what char n-grams buy,
    without the vocabulary blow-up."""
    return {_stem(w) for w in words(text)}


def words_and_char4(text: str) -> set[str]:
    """Union. Word tokens keep the precise signal; n-grams add the fuzzy
    one. The risk is that the n-grams swamp the words in the union and the
    precision of the word half stops mattering."""
    return words(text) | {"#" + g for g in char4(text)}


CANDIDATES = {
    "words (shipped)": words,
    "stems": stems,
    "char4": char4,
    "char5": char5,
    "words+char4": words_and_char4,
}


# --- Scoring -------------------------------------------------------------


def signature(tokens: set[str]) -> list[int]:
    """MinHash over an arbitrary token set, using production's own
    permutations and hash so the similarity being estimated is the same
    quantity the Hub estimates."""
    if not tokens:
        # Production draws randomly here so two empty texts never compare as
        # identical. A fixed sentinel is fine in an evaluation and keeps runs
        # reproducible; no corpus record or probe is empty in practice.
        return [0] * NUM_PERM
    hashes = [_stable_hash(t) for t in tokens]
    return [min((a * h + b) % _MERSENNE_61 for h in hashes) for a, b in _permutations(NUM_PERM)]


def _load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def score(tokenize, probes: list[dict], corpus: list[dict], threshold: float = THRESHOLD) -> dict:
    titles = [r["title"] for r in corpus]
    corpus_sigs = [
        signature(tokenize(commons.matchable_text(r["title"], r.get("context_text", ""), r.get("tags"))))
        for r in corpus
    ]

    hits = right = false_pos = 0
    rank1 = 0          # right record ranked first, THRESHOLD IGNORED
    sims_of_truth = []  # similarity to the correct record, for every positive
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
            # Ranking quality, measured independently of the threshold. If
            # the right record is already ranked first and merely sits below
            # 0.30, the knowledge IS findable and the binary verdict is what
            # hides it -- a different problem, with a different fix, than a
            # matcher that cannot rank at all.
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
        # statistics.median averages the two middle values on an even-length
        # list; sims_of_truth[len // 2] instead always picked the
        # upper-middle element outright, silently skewing the reported
        # median toward higher similarities whenever the probe count was even.
        "median_true_sim": statistics.median(sims_of_truth) if sims_of_truth else 0.0,
        "recall": hits / n_pos if n_pos else 0.0,
        "right_of_hits": right / hits if hits else 0.0,
        "false_positive": false_pos / n_neg if n_neg else 0.0,
        "hits": hits, "n_pos": n_pos, "false_pos": false_pos, "n_neg": n_neg,
    }


def main() -> int:
    corpus = _load(CORPUS)
    dev = _load(HERE / "probes-v1.jsonl")
    held = _load(HERE / "probes-v2.jsonl")

    print(f"corpus {len(corpus)} records | threshold {THRESHOLD} (shipped, unchanged) "
          f"| metric estimate_jaccard (unchanged)")
    print(f"dev = probes-v1 ({sum(1 for p in dev if p['expect']=='covered')}+"
          f"{sum(1 for p in dev if p['expect']=='uncovered')})  "
          f"held-out = probes-v2 ({sum(1 for p in held if p['expect']=='covered')}+"
          f"{sum(1 for p in held if p['expect']=='uncovered')})")
    print()
    print(f"{'representation':<18} {'dev recall':>11} {'dev FP':>8} "
          f"{'HELD recall':>12} {'HELD FP':>9} {'rank1':>7} {'med sim':>8}")
    print("-" * 82)

    rows = {}
    for name, fn in CANDIDATES.items():
        d = score(fn, dev, corpus)
        h = score(fn, held, corpus)
        rows[name] = (d, h)
        print(f"{name:<18} {d['recall']:>10.1%} {d['false_positive']:>8.1%} "
              f"{h['recall']:>11.1%} {h['false_positive']:>9.1%} "
              f"{h['rank1']:>7.0%} {h['median_true_sim']:>8.3f}")

    print()
    base_d, base_h = rows["words (shipped)"]
    print("Read the HELD columns, not the dev ones. A candidate is only better if")
    print("recall rises AND false positives stay at zero -- recall bought with false")
    print("positives is the bar dropping, not the matcher improving.")
    print()
    print("rank1 = the correct record was ranked FIRST, threshold ignored.")
    print("med sim = median similarity between a probe and the record it targets.")
    print()
    for name, (_d, h) in rows.items():
        if name == "words (shipped)":
            continue
        dr = h["recall"] - base_h["recall"]
        dfp = h["false_positive"] - base_h["false_positive"]
        verdict = (
            "REAL GAIN" if dr > 0 and dfp <= 0
            else "gain paid for with false positives" if dr > 0
            else "no gain"
        )
        print(f"  {name:<16} recall {dr:+.1%}, false positives {dfp:+.1%}  -> {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
