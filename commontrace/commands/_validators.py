"""Shared argparse `type=` validators for CLI flags reused across commands."""
from __future__ import annotations

import argparse
import sys

from commontrace import paths


def agent_type(value: str) -> str:
    """Any slug is a valid agent_type; this checks shape, not membership."""
    candidate = value.strip().lower()
    if not paths.AGENT_TYPE_RE.match(candidate):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a valid agent type: expected a lowercase slug "
            f"matching {paths.AGENT_TYPE_RE.pattern} (letters, digits, '_', '-'). "
            f"Any field is valid -- e.g. {', '.join(paths.SUGGESTED_AGENT_TYPES[:4])}, "
            "robotics, legal -- the taxonomy is open (protocol/PROTOCOL.md §7)."
        )
    return candidate


def similarity_threshold(value: str) -> float:
    try:
        threshold = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a number, got {value!r}") from None
    if not (0 < threshold <= 1):
        raise argparse.ArgumentTypeError(
            f"must be > 0 and <= 1 (Jaccard similarity's own range), got {threshold}"
        )
    return threshold


WARN_CHARS = 256 * 1024
REFUSE_CHARS = 1024 * 1024


def check_text_size(fields: dict[str, str], *, what: str) -> bool:
    """Warn (return True) or refuse (return False) an oversized write."""
    total = sum(len(text or "") for text in fields.values())
    if total > REFUSE_CHARS:
        biggest = sorted(fields.items(), key=lambda kv: len(kv[1] or ""), reverse=True)[:3]
        detail = ", ".join(f"{name} ({len(text or '')} chars)" for name, text in biggest)
        print(
            f"[commontrace] refusing to write an oversized {what} "
            f"({total} chars total: {detail}; limit {REFUSE_CHARS}). "
            "Split the content or store large logs/traces outside the lesson.",
            file=sys.stderr,
        )
        return False
    if total > WARN_CHARS:
        print(
            f"[commontrace] warning: {what} is {total} chars; large lessons/traces "
            "slow retrieval and index builds. Consider trimming.",
            file=sys.stderr,
        )
    return True
