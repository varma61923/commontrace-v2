"""Per-session cost and token ledger with per-model attribution.

Adapted from Cognee session ledger architecture (#3 L):
- Tracks every LLM model call, token expenditure (prompt + completion), and estimated cost.
- Groups and attributes usage per session, per model, and per occasion (e.g. recall, extraction, answer).
- Provides instant session-level and global financial observability for agent workflows.
"""

from __future__ import annotations

import math
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from unimem import _jsonl, paths

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
    # Provider paths and dated aliases identify the same model. Match the full
    # family identifier so gpt-4o cannot shadow gpt-4o-mini, and arbitrary partial
    # names such as "gpt" or "mini" do not silently acquire another model's rate.
    m_clean = m_clean.rsplit("/", 1)[-1]
    m_clean = re.sub(r"^(?:(?:us|eu|apac)\.)?(?:openai|anthropic|google)[:.]", "", m_clean)
    rate = DEFAULT_PRICING_PER_MILLION["default"]

    for key in sorted(DEFAULT_PRICING_PER_MILLION, key=len, reverse=True):
        if m_clean == key or re.fullmatch(re.escape(key) + r"-(?:\d[\w.:-]*|latest|preview)", m_clean):
            rate = DEFAULT_PRICING_PER_MILLION[key]
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


def _finite_cost(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{field_name} must be a finite nonnegative number")
    try:
        cost = float(value)
    except OverflowError:
        raise ValueError(f"{field_name} must be a finite nonnegative number") from None
    if not math.isfinite(cost) or cost < 0:
        raise ValueError(f"{field_name} must be a finite nonnegative number")
    return cost


def _validate_entry(entry: LedgerEntry) -> None:
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(entry, name)
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    if entry.total_tokens != entry.prompt_tokens + entry.completion_tokens:
        raise ValueError("total_tokens does not match prompt_tokens + completion_tokens")
    _finite_cost(entry.cost_usd, "cost_usd")
    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    if (
        metadata.get("cache_hit") is True
        and metadata.get("historical_cost_known", metadata.get("cost_known", True)) is not False
    ):
        _finite_cost(metadata.get("historical_cost_usd", entry.cost_usd), "historical_cost_usd")


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
    try:
        c_usd = float(cost_usd) if cost_usd is not None else estimate_cost(m_str, p_toks, c_toks)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("cost must be a finite nonnegative number") from None
    if isinstance(cost_usd, bool) or not math.isfinite(c_usd) or c_usd < 0:
        raise ValueError("cost must be a finite nonnegative number")

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
    _validate_entry(entry)

    path = _ledger_file(root)
    with _jsonl.locked(_lock_file(root)):
        _jsonl.append_row(path, entry.to_dict())

    return entry


def _new_usage_stat() -> dict[str, Any]:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_hit_calls": 0,
        "cached_tokens": 0,
        "known_cost_usd": 0.0,
        "unpriced_calls": 0,
        "historical_cached_cost_usd": 0.0,
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
        stat["known_cost_usd"] = round(_finite_cost(stat["known_cost_usd"] + entry.cost_usd, "total cost"), 6)
    if cache_hit:
        stat["cache_hit_calls"] += 1
        stat["cached_tokens"] += entry.total_tokens
        history_known = metadata.get("historical_cost_known", metadata.get("cost_known", True)) is not False
        if history_known:
            history_cost = metadata.get("historical_cost_usd", entry.cost_usd)
            stat["historical_cached_cost_usd"] = round(
                _finite_cost(stat["historical_cached_cost_usd"] + float(history_cost), "historical total cost"), 6
            )
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
        if not isinstance(r, dict):
            continue
        if clean_session and r.get("session_id") != clean_session:
            continue
        try:
            entry = LedgerEntry(**r)
            _validate_entry(entry)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"corrupt session ledger entry: {exc}") from None
        entries.append(entry)
        if len(entries) >= limit:
            break

    return entries
