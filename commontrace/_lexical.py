from __future__ import annotations

import re

WORD_RE = re.compile(r"\w+", re.UNICODE)

STOPWORDS = frozenset(
    """
    a an the of to in on for with and or but is are was were be been being
    this that these those it its as at by from into over under again
    further then once here there when where why how all any both each
    few more most other some such no nor not only own same so than too
    very can will just don should now i you he she we they them his her
    """.split()
)

# CJK bigram lane: WORD_RE keeps a Han/Hangul/Kana compound (e.g. "深度学习")
# as one token, so a query sharing only part of the compound never matches.
# Splitting CJK runs into overlapping character bigrams gives partial-compound
# recall while leaving the ASCII/English path completely untouched.
_CJK_RE = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\u3040-\u309f\u30a0-\u30ff\uff66-\uff9d"
    "\uac00-\ud7af\ua960-\ua97f]"
)


def has_cjk(text: str) -> bool:
    """True when `text` contains any CJK (Han/Hangul/Kana) character."""
    return _CJK_RE.search(text) is not None


def segment_cjk(token: str) -> list[str]:
    """Split `token`'s CJK runs into overlapping character bigrams.

    Non-CJK runs pass through as single tokens; a CJK run shorter than
    two characters passes through unchanged. Pure-ASCII input returns
    `[token]`, so the English path is byte-identical with or without
    this lane.
    """
    if not has_cjk(token):
        return [token]
    out: list[str] = []
    buf: list[str] = []

    def _flush_cjk() -> None:
        if not buf:
            return
        if len(buf) < 2:
            out.append("".join(buf))
        else:
            out.extend(
                "".join(buf[i:i + 2]) for i in range(len(buf) - 1)
            )
        del buf[:]

    latin: list[str] = []

    def _flush_latin() -> None:
        if latin:
            out.append("".join(latin))
            del latin[:]

    for ch in token:
        if _CJK_RE.match(ch):
            _flush_latin()
            buf.append(ch)
        else:
            _flush_cjk()
            latin.append(ch)
    _flush_cjk()
    _flush_latin()
    return out or [token]
