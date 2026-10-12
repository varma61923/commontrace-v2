"""Per-session cost and token ledger with per-model attribution.

Adapted from Cognee session ledger architecture (#3 L):
- Tracks every LLM model call, token expenditure (prompt + completion), and estimated cost.
- Groups and attributes usage per session, per model, and per occasion (e.g. recall, extraction, answer).
- Provides instant session-level and global financial observability for agent workflows.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from commontrace import _jsonl, paths

# Model pricing in USD per 1,000,000 tokens (input_price, output_price)
DEFAULT_PRICING_PER_MILLION: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-haiku": (0.25, 1.25),
    "claude-haiku": (0.25, 1.25),
    "claude-opus": (15.00, 75.00),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-2.0-flash": (0.10, 0.40),
    "default": (1.00, 3.00),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_file(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "session_ledger.jsonl")


def _lock_file(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "session_ledger")


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate estimated cost in USD based on model identifier and token counts."""
    m_clean = str(model or "").lower().strip()
    rate = DEFAULT_PRICING_PER_MILLION.get("default")

    for key, (in_rate, out_rate) in DEFAULT_PRICING_PER_MILLION.items():
        if key in m_clean or m_clean in key:
            rate = (in_rate, out_rate)
            break

    in_rate, out_rate = rate
    cost = (prompt_tokens * in_rate + completion_tokens * out_rate) / 1_000_000.0
    return round(cost, 6)


@dataclass
class LedgerEntry:
    id: str
    session_id: str
    timestamp: str
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    occasion: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def record_usage(
    root: str,
    session_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    provider: str = "",
    cost_usd: float | None = None,
    occasion: str = "",
    metadata: dict[str, Any] | None = None,
) -> LedgerEntry:
    """Record an LLM model call with tokens and cost attribution to a session."""
    p_toks = max(0, int(prompt_tokens))
    c_toks = max(0, int(completion_tokens))
    tot_toks = p_toks + c_toks

    m_str = str(model or "unknown").strip()
    c_usd = cost_usd if cost_usd is not None else estimate_cost(m_str, p_toks, c_toks)

    entry = LedgerEntry(
        id=f"leg_{uuid.uuid4().hex[:12]}",
        session_id=str(session_id or "default").strip(),
        timestamp=_now(),
        model=m_str,
        provider=str(provider or "").strip().lower(),
        prompt_tokens=p_toks,
        completion_tokens=c_toks,
        total_tokens=tot_toks,
        cost_usd=round(float(c_usd), 6),
        occasion=str(occasion or "").strip().lower(),
        metadata=dict(metadata or {}),
    )

    path = _ledger_file(root)
    with _jsonl.locked(_lock_file(root)):
        _jsonl.append_row(path, entry.to_dict())

    return entry


def _new_usage_stat() -> dict[str, Any]:
    return {
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cache_hit_calls": 0, "cached_tokens": 0, "known_cost_usd": 0.0,
        "unpriced_calls": 0, "historical_cached_cost_usd": 0.0,
        "cached_unpriced_calls": 0,
    }


def _add_usage(stat: dict[str, Any], entry: LedgerEntry) -> None:
    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    cache_hit = metadata.get("cache_hit") is True
    stat["calls"] += 1
    stat["prompt_tokens"] += entry.prompt_tokens
    stat["completion_tokens"] += entry.completion_tokens
    stat["total_tokens"] += entry.total_tokens
    if metadata.get("cost_known", True) is False:
        stat["unpriced_calls"] += 1
    else:
        stat["known_cost_usd"] = round(stat["known_cost_usd"] + entry.cost_usd, 6)
    if cache_hit:
        stat["cache_hit_calls"] += 1
        stat["cached_tokens"] += entry.total_tokens
        history_known = metadata.get("historical_cost_known", metadata.get("cost_known", True)) is not False
        if history_known:
            history_cost = metadata.get("historical_cost_usd", entry.cost_usd)
            if isinstance(history_cost, (float, int)) and not isinstance(history_cost, bool):
                stat["historical_cached_cost_usd"] = round(
                    stat["historical_cached_cost_usd"] + float(history_cost), 6)
        else:
            stat["cached_unpriced_calls"] += 1


def _finish_usage_stat(stat: dict[str, Any]) -> dict[str, Any]:
    stat["cost_usd"] = stat["known_cost_usd"] if stat["unpriced_calls"] == 0 else None
    if stat["cached_unpriced_calls"]:
        stat["historical_cached_cost_usd"] = None
    return stat


def session_summary(root: str, session_id: str) -> dict[str, Any]:
    """Aggregate token usage and costs for a given session with per-model attribution."""
    clean_session = str(session_id or "").strip()
    entries = list_session_entries(root, session_id=clean_session, limit=10_000)

    total_prompt = 0
    total_completion = 0
    overall = _new_usage_stat()
    by_model: dict[str, dict[str, Any]] = {}
    by_occasion: dict[str, dict[str, Any]] = {}

    for e in entries:
        total_prompt += e.prompt_tokens
        total_completion += e.completion_tokens
        _add_usage(overall, e)

        # By model
        _add_usage(by_model.setdefault(e.model, _new_usage_stat()), e)

        # By occasion
        occ = e.occasion or "unspecified"
        _add_usage(by_occasion.setdefault(occ, _new_usage_stat()), e)

    for stat in by_model.values():
        _finish_usage_stat(stat)
    for stat in by_occasion.values():
        _finish_usage_stat(stat)
    _finish_usage_stat(overall)

    return {
        "session_id": clean_session,
        "call_count": len(entries),
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "total_tokens": total_prompt + total_completion,
        "total_cost_usd": overall["cost_usd"],
        "known_cost_usd": overall["known_cost_usd"],
        "unpriced_calls": overall["unpriced_calls"],
        "cache_hit_count": overall["cache_hit_calls"],
        "cached_tokens": overall["cached_tokens"],
        "historical_cached_cost_usd": overall["historical_cached_cost_usd"],
        "cached_unpriced_calls": overall["cached_unpriced_calls"],
        "by_model": by_model,
        "by_occasion": by_occasion,
    }


def overall_ledger_summary(
    root: str,
    since: str = "",
    until: str = "",
) -> dict[str, Any]:
    """Aggregate token usage and costs across all sessions, optionally bounded by date."""
    entries = list_session_entries(root, limit=100_000)

    total_prompt = 0
    total_completion = 0
    overall = _new_usage_stat()
    sessions_seen: set[str] = set()
    by_model: dict[str, dict[str, Any]] = {}
    filtered_count = 0

    for e in entries:
        if since and e.timestamp < since:
            continue
        if until and e.timestamp > until:
            continue

        sessions_seen.add(e.session_id)
        filtered_count += 1
        total_prompt += e.prompt_tokens
        total_completion += e.completion_tokens
        _add_usage(overall, e)
        _add_usage(by_model.setdefault(e.model, _new_usage_stat()), e)

    for stat in by_model.values():
        _finish_usage_stat(stat)
    _finish_usage_stat(overall)

    return {
        "call_count": filtered_count,
        "session_count": len(sessions_seen),
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "total_tokens": total_prompt + total_completion,
        "total_cost_usd": overall["cost_usd"],
        "known_cost_usd": overall["known_cost_usd"],
        "unpriced_calls": overall["unpriced_calls"],
        "cache_hit_count": overall["cache_hit_calls"],
        "cached_tokens": overall["cached_tokens"],
        "historical_cached_cost_usd": overall["historical_cached_cost_usd"],
        "cached_unpriced_calls": overall["cached_unpriced_calls"],
        "by_model": by_model,
    }


def list_session_entries(
    root: str,
    session_id: str = "",
    limit: int = 100,
) -> list[LedgerEntry]:
    """List ledger entries, optionally filtered by session ID."""
    path = _ledger_file(root)
    rows = _jsonl.read_rows(path)
    clean_session = session_id.strip() if session_id else ""

    entries: list[LedgerEntry] = []
    for r in reversed(rows):
        if not isinstance(r, dict) or not r.get("id"):
            continue
        if clean_session and r.get("session_id") != clean_session:
            continue
        try:
            entries.append(LedgerEntry(**r))
        except TypeError:
            continue
        if len(entries) >= limit:
            break

    return entries
