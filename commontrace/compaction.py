"""Transcript compaction with lookup hints (pure API).

Sliding-window summarizer for agent transcripts: the caller keeps the last
``keep_pct`` of messages verbatim and this module compacts the evicted
prefix into a 7-section summary dict:

- ``what_happened``: narrative of the evicted window (str)
- ``key_decisions``: decisions taken in the evicted window (list[str])
- ``open_state``: unresolved items / pending work (list[str])
- ``lookup_hints``: entity tokens consumable by the retrieval entity index
  (list[str], see :func:`commontrace.retrieval.build_entity_index`)
- ``kept_messages``: the verbatim tail window (list[dict])
- ``evicted_count``: how many messages were compacted away (int)
- ``method``: ``"llm"`` or ``"extractive"`` (str)

Summarization prefers the configured LLM (via :mod:`commontrace.llm`) and
falls back to a stdlib extractive summarizer (first/last-N + term-frequency
sentences) whenever the LLM is unavailable or its reply is unusable.

This module is deliberately side-effect free: no file IO, no imports from
``agent loop`` (the lead wires this API into the loop).
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any

from commontrace import llm as _llm
from commontrace import retrieval as _retrieval
from commontrace._lexical import STOPWORDS as _STOPWORDS
from commontrace._lexical import WORD_RE as _WORD_RE

__all__ = [
    "MODE_SLIDING_WINDOW",
    "METHOD_LLM",
    "METHOD_EXTRACTIVE",
    "SUMMARY_KEYS",
    "SummaryDict",
    "normalize_messages",
    "split_window",
    "extractive_summarize",
    "lookup_hints_for",
    "render_markdown",
    "summarize_messages",
]

MODE_SLIDING_WINDOW = "sliding_window"
METHOD_LLM = "llm"
METHOD_EXTRACTIVE = "extractive"

SUMMARY_KEYS = (
    "what_happened",
    "key_decisions",
    "open_state",
    "lookup_hints",
    "kept_messages",
    "evicted_count",
    "method",
)

# Hyphenated aliases read the same underlying keys without adding entries,
# so ``result["what-happened"]`` works while ``set(result)`` stays canonical.
_ALIASES = {
    "what-happened": "what_happened",
    "key-decisions": "key_decisions",
    "open-state": "open_state",
    "lookup-hints": "lookup_hints",
    "kept-messages": "kept_messages",
    "evicted-count": "evicted_count",
}

_MAX_HINTS_DEFAULT = 64
_EDGE_SENTENCES = 2  # first-N / last-N sentences kept by the extractive path
_TOP_TF_SENTENCES = 5  # term-frequency sentences kept by the extractive path
_MAX_LIST_ITEMS = 5


class SummaryDict(dict):
    """A plain dict with hyphen-tolerant reads for the 7 summary sections."""

    @staticmethod
    def _canon(key: Any) -> Any:
        if isinstance(key, str):
            return _ALIASES.get(key, key)
        return key

    def __getitem__(self, key: Any) -> Any:
        return super().__getitem__(self._canon(key))

    def get(self, key: Any, default: Any = None) -> Any:  # noqa: D102
        return super().get(self._canon(key), default)

    def __contains__(self, key: Any) -> bool:  # noqa: D105
        return super().__contains__(self._canon(key))


# ---------------------------------------------------------------------------
# Message normalization + windowing
# ---------------------------------------------------------------------------

def _text_of(item: Any) -> tuple[str, str]:
    """Normalize one message-ish item to a (role, content) pair."""
    if isinstance(item, dict):
        role = str(item.get("role", "user") or "user")
        for key in ("content", "text", "body"):
            if item.get(key) not in (None, ""):
                return role, str(item[key])
        return role, ""
    if isinstance(item, str):
        return "user", item
    role = str(getattr(item, "role", "user") or "user")
    content = getattr(item, "content", None)
    if content is None:
        content = getattr(item, "text", "")
    return role, str(content or "")


def normalize_messages(messages: Any) -> list[dict]:
    """Normalize dicts / AgentTurn-likes / strings to [{role, content}]."""
    if messages is None:
        return []
    return [{"role": role, "content": content} for role, content in (_text_of(m) for m in messages)]


def _normalize_mode(mode: str) -> str:
    name = str(mode or "").strip().lower().replace("-", "_")
    if name != MODE_SLIDING_WINDOW:
        raise ValueError(f"unsupported compaction mode {mode!r} (only 'sliding_window')")
    return MODE_SLIDING_WINDOW


def _validate_keep_pct(keep_pct: float) -> float:
    try:
        pct = float(keep_pct)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"keep_pct must be a number in [0, 1], got {keep_pct!r}") from exc
    if not 0.0 <= pct <= 1.0:
        raise ValueError(f"keep_pct must be in [0, 1], got {keep_pct!r}")
    return pct


def split_window(
    messages: list[dict], keep_pct: float
) -> tuple[list[dict], list[dict]]:
    """Split normalized messages into (evicted, kept) for a sliding window."""
    pct = _validate_keep_pct(keep_pct)
    n = len(messages)
    n_keep = 0 if pct <= 0.0 else int(math.ceil(n * pct))
    n_keep = max(0, min(n, n_keep))
    cut = n - n_keep
    return list(messages[:cut]), list(messages[cut:])


# ---------------------------------------------------------------------------
# Extractive fallback: first/last-N + term-frequency sentences
# ---------------------------------------------------------------------------

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_DECISION_RE = re.compile(
    r"\b(decid\w*|agre\w*|cho[os]\w*|approv\w*|confirm\w*|merg\w*|"
    r"resolv\w*|fix\w*|shipp\w*|landed|will\b)", re.IGNORECASE)
_OPEN_RE = re.compile(
    r"\b(todo|pend\w*|next|open|unresolv\w*|follow[\s-]?up|"
    r"need(?:s|ed)? to|should|question|block\w*|remain\w*|still)\b",
    re.IGNORECASE)


def _split_sentences(texts: list[str]) -> list[str]:
    out: list[str] = []
    for text in texts:
        for piece in _SENT_SPLIT_RE.split(text or ""):
            sentence = " ".join(piece.split())
            if sentence:
                out.append(sentence)
    return out


def _content_tokens(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1]


def _top_tf_indices(sentences: list[str], limit: int) -> list[int]:
    freq: Counter = Counter()
    tokenized = [_content_tokens(s) for s in sentences]
    for toks in tokenized:
        freq.update(toks)
    if not freq:
        return []
    scored = sorted(
        range(len(sentences)),
        key=lambda i: (-sum(freq[t] for t in tokenized[i]), i),
    )
    return sorted(scored[: max(0, limit)])


def extractive_summarize(evicted_texts: list[str]) -> tuple[str, list[str], list[str]]:
    """Extractive (what_happened, key_decisions, open_state) for evicted texts."""
    sentences = _split_sentences([t for t in (evicted_texts or []) if (t or "").strip()])
    if not sentences:
        return "", [], []
    edge = set(range(min(_EDGE_SENTENCES, len(sentences))))
    edge.update(range(max(0, len(sentences) - _EDGE_SENTENCES), len(sentences)))
    chosen = sorted(set(edge) | set(_top_tf_indices(sentences, _TOP_TF_SENTENCES)))
    what_happened = " ".join(sentences[i] for i in chosen)
    key_decisions = [s for s in sentences if _DECISION_RE.search(s)][: _MAX_LIST_ITEMS]
    open_state = [s for s in sentences if _OPEN_RE.search(s)][: _MAX_LIST_ITEMS]
    return what_happened, key_decisions, open_state


# ---------------------------------------------------------------------------
# Lookup hints: entity tokens consumable by the retrieval entity index
# ---------------------------------------------------------------------------

def lookup_hints_for(evicted_texts: list[str], max_hints: int = _MAX_HINTS_DEFAULT) -> list[str]:
    """Entity tokens for the evicted window, ordered by (-frequency, token).

    Built with :func:`commontrace.retrieval._entity_tokens_for_query`, so a
    query of ``" ".join(hints)`` reproduces the same tokens and lessons
    indexed by :func:`commontrace.retrieval.build_entity_index` match them.
    """
    joined = "\n".join(t for t in (evicted_texts or []) if (t or "").strip())
    if not joined.strip():
        return []
    tokens = _retrieval._entity_tokens_for_query(joined)
    if not tokens:
        return []
    freq = Counter(_WORD_RE.findall(joined.lower()))
    ordered = sorted(tokens, key=lambda tok: (-freq.get(tok, 0), tok))
    try:
        limit = int(max_hints)
    except (TypeError, ValueError):
        limit = _MAX_HINTS_DEFAULT
    if limit < 0:
        limit = 0
    return ordered[:limit]


# ---------------------------------------------------------------------------
# LLM path via commontrace.llm, with graceful fallback to extractive
# ---------------------------------------------------------------------------

_COMPACTION_PROMPT = (
    "You compact an evicted transcript window so a future agent turn can "
    "recover its facts. Reply with a single JSON object and nothing else, "
    'with exactly these keys: {"what_happened": str, "key_decisions": [str], '
    '"open_state": [str]}.\nTranscript:\n'
)

_CALLERS = {
    "anthropic": "_call_anthropic",
    "openai-compatible": "_call_openai_compatible",
    "bedrock": "_call_bedrock",
    "vertex": "_call_vertex",
}


def _extract_json_object_or_none(text: str) -> dict | None:
    stripped = (text or "").strip()
    if not stripped:
        return None
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        try:
            parsed = json.loads(stripped[start: end + 1])
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            return None
    return None


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        text = str(item or "").strip() if not isinstance(item, str) else item.strip()
        if text:
            out.append(text)
    return out


def _llm_summarize(evicted_texts: list[str]) -> dict | None:
    """Summarize evicted texts with the configured LLM, or None when unusable."""
    try:
        config = _llm.load_config()
    except Exception:
        return None
    caller = getattr(_llm, _CALLERS.get(config.provider, ""), None)
    if caller is None:
        return None
    try:
        text, _usage = caller(config, _COMPACTION_PROMPT + "\n".join(evicted_texts))
    except Exception:
        return None
    parsed = _extract_json_object_or_none(text)
    if not parsed:
        return None
    what = parsed.get("what_happened")
    if not isinstance(what, str) or not what.strip():
        return None
    return {
        "what_happened": what.strip(),
        "key_decisions": _as_str_list(parsed.get("key_decisions"))[: _MAX_LIST_ITEMS],
        "open_state": _as_str_list(parsed.get("open_state"))[: _MAX_LIST_ITEMS],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def summarize_messages(
    messages: Any,
    mode: str = MODE_SLIDING_WINDOW,
    keep_pct: float = 0.3,
    *,
    max_hints: int = _MAX_HINTS_DEFAULT,
    llm_enabled: bool = True,
) -> SummaryDict:
    """Compact a transcript with a sliding window.

    Args:
        messages: dicts ({role, content}) / AgentTurn-likes / strings.
        mode: only ``"sliding_window"`` is supported.
        keep_pct: fraction of the tail kept verbatim, in [0, 1].
        max_hints: cap on ``lookup_hints`` tokens.
        llm_enabled: when False, skip the LLM and use the extractive path.

    Returns:
        A 7-section :class:`SummaryDict` (see module docstring).
    """
    _normalize_mode(mode)
    pct = _validate_keep_pct(keep_pct)
    normalized = normalize_messages(messages)
    evicted, kept = split_window(normalized, pct)
    evicted_texts = [m["content"] for m in evicted if (m.get("content") or "").strip()]

    method = METHOD_EXTRACTIVE
    what_happened, key_decisions, open_state = extractive_summarize(evicted_texts)
    if llm_enabled and evicted_texts:
        llm_part = _llm_summarize(evicted_texts)
        if llm_part is not None:
            what_happened = llm_part["what_happened"]
            key_decisions = llm_part["key_decisions"]
            open_state = llm_part["open_state"]
            method = METHOD_LLM

    return SummaryDict({
        "what_happened": what_happened,
        "key_decisions": list(key_decisions),
        "open_state": list(open_state),
        "lookup_hints": lookup_hints_for(evicted_texts, max_hints=max_hints),
        "kept_messages": [dict(m) for m in kept],
        "evicted_count": len(evicted),
        "method": method,
    })


def render_markdown(summary: dict) -> str:
    """Render a 7-section markdown compaction note from a summary dict."""
    get = summary.get if isinstance(summary, dict) else (lambda k, d=None: d)
    lines = ["# Transcript Compaction", ""]
    lines += ["## What Happened", "", str(get("what_happened") or "_Nothing evicted._"), ""]
    for title, key in (("Key Decisions", "key_decisions"),
                       ("Open State", "open_state"),
                       ("Lookup Hints", "lookup_hints")):
        items = get(key) or []
        lines += [f"## {title}", ""]
        if items:
            lines += [f"- {item}" for item in items]
        else:
            lines += ["_None recorded._"]
        lines += [""]
    kept = get("kept_messages") or []
    lines += ["## Kept Messages", ""]
    if kept:
        for msg in kept:
            role = msg.get("role", "user") if isinstance(msg, dict) else "user"
            content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
            lines += [f"- **{role}:** {content}"]
    else:
        lines += ["_No messages kept._"]
    lines += [""]
    lines += ["## Evicted Count", "", str(get("evicted_count", 0)), ""]
    lines += ["## Method", "", str(get("method", METHOD_EXTRACTIVE)), ""]
    return "\n".join(lines).rstrip() + "\n"
