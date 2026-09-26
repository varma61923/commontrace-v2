"""The Porter stemmer (M.F. Porter, "An algorithm for suffix stripping",
Program 14(3), 1980), stdlib only.

Used by the `idf-v3` lexical scorer (commontrace/retrieval.py) so that
"reset", "resets", "resetting" and "reset's" are one term. Without it a
lesson written as "retry the failed upload" and a task described as
"uploads keep failing, retrying" share nothing but stopwords.

The original algorithm, implemented from the paper, rather than a library:
this package is stdlib-first and must not grow a dependency for one
function. tests/test_stem.py pins the behaviour against the paper's own
worked examples and the published reference vocabulary.

`stem` is memoized: a store's vocabulary is small and the same words recur
on every retrieval.
"""

from __future__ import annotations

from functools import lru_cache

_VOWELS = frozenset("aeiou")


def _is_consonant(word: str, i: int) -> bool:
    ch = word[i]
    if ch in _VOWELS:
        return False
    if ch == "y":
        return i == 0 or not _is_consonant(word, i - 1)
    return True


def _measure(stem: str) -> int:
    """m in [C](VC)^m[V]: the number of vowel-consonant sequences."""
    m = 0
    i, n = 0, len(stem)
    while i < n and _is_consonant(stem, i):
        i += 1
    while i < n:
        while i < n and not _is_consonant(stem, i):
            i += 1
        if i >= n:
            break
        while i < n and _is_consonant(stem, i):
            i += 1
        m += 1
    return m


def _has_vowel(stem: str) -> bool:
    return any(not _is_consonant(stem, i) for i in range(len(stem)))


def _ends_double_consonant(word: str) -> bool:
    return (
        len(word) >= 2 and word[-1] == word[-2] and _is_consonant(word, len(word) - 1)
    )


def _cvc(word: str) -> bool:
    """*o: ends consonant-vowel-consonant, the last not w, x or y."""
    if len(word) < 3:
        return False
    if not (_is_consonant(word, len(word) - 3) and not _is_consonant(word, len(word) - 2)
            and _is_consonant(word, len(word) - 1)):
        return False
    return word[-1] not in "wxy"


def _replace(word: str, suffix: str, replacement: str, min_measure: int) -> str | None:
    """`word` with `suffix` swapped for `replacement` if the stem's measure
    exceeds `min_measure`; None if the suffix does not match at all (so the
    caller stops looking), `word` unchanged if it matched but failed m."""
    if not word.endswith(suffix):
        return None
    stem = word[: len(word) - len(suffix)]
    if _measure(stem) > min_measure:
        return stem + replacement
    return word


def _step1a(w: str) -> str:
    if w.endswith("sses"):
        return w[:-2]
    if w.endswith("ies"):
        return w[:-2]
    if w.endswith("ss"):
        return w
    if w.endswith("s"):
        return w[:-1]
    return w


def _step1b(w: str) -> str:
    if w.endswith("eed"):
        stem = w[:-3]
        return stem + "ee" if _measure(stem) > 0 else w
    for suffix in ("ed", "ing"):
        if w.endswith(suffix):
            stem = w[: -len(suffix)]
            if not _has_vowel(stem):
                return w
            if stem.endswith(("at", "bl", "iz")):
                return stem + "e"
            if _ends_double_consonant(stem) and stem[-1] not in "lsz":
                return stem[:-1]
            if _measure(stem) == 1 and _cvc(stem):
                return stem + "e"
            return stem
    return w


def _step1c(w: str) -> str:
    if w.endswith("y") and _has_vowel(w[:-1]):
        return w[:-1] + "i"
    return w


_STEP2 = (
    ("ational", "ate"), ("tional", "tion"), ("enci", "ence"), ("anci", "ance"),
    ("izer", "ize"), ("abli", "able"), ("alli", "al"), ("entli", "ent"), ("eli", "e"),
    ("ousli", "ous"), ("ization", "ize"), ("ation", "ate"), ("ator", "ate"),
    ("alism", "al"), ("iveness", "ive"), ("fulness", "ful"), ("ousness", "ous"),
    ("aliti", "al"), ("iviti", "ive"), ("biliti", "ble"),
)

_STEP3 = (
    ("icate", "ic"), ("ative", ""), ("alize", "al"), ("iciti", "ic"), ("ical", "ic"),
    ("ful", ""), ("ness", ""),
)

_STEP4 = (
    "al", "ance", "ence", "er", "ic", "able", "ible", "ant", "ement", "ment", "ent",
    "ion", "ou", "ism", "ate", "iti", "ous", "ive", "ize",
)


def _step_table(w: str, table) -> str:
    # The paper's rule: only the LONGEST matching suffix is considered, and
    # if its condition fails the word is left alone.
    for suffix, replacement in sorted(table, key=lambda p: -len(p[0])):
        out = _replace(w, suffix, replacement, 0)
        if out is not None:
            return out
    return w


def _step4(w: str) -> str:
    for suffix in sorted(_STEP4, key=len, reverse=True):
        if not w.endswith(suffix):
            continue
        stem = w[: -len(suffix)]
        if suffix == "ion" and not stem.endswith(("s", "t")):
            return w
        return stem if _measure(stem) > 1 else w
    return w


def _step5(w: str) -> str:
    if w.endswith("e"):
        stem = w[:-1]
        m = _measure(stem)
        if m > 1 or (m == 1 and not _cvc(stem)):
            w = stem
    if _measure(w) > 1 and _ends_double_consonant(w) and w.endswith("l"):
        w = w[:-1]
    return w


@lru_cache(maxsize=65536)
def stem(word: str) -> str:
    """Porter-stem one lower-case word. Words of two letters or fewer, and
    anything that is not purely alphabetic (identifiers, numbers, error
    codes), are returned unchanged: `http2`, `e2e` and `0x80070005` are
    exact tokens, and stemming them could only merge things that differ."""
    if len(word) <= 2 or not word.isalpha() or not word.isascii():
        return word
    w = _step1a(word)
    w = _step1b(w)
    w = _step1c(w)
    w = _step_table(w, _STEP2)
    w = _step_table(w, _STEP3)
    w = _step4(w)
    w = _step5(w)
    return w
