"""Shared argparse `type=` validators for CLI flags reused across commands."""
from __future__ import annotations

import argparse
import sys

from commontrace import paths


def agent_type(value: str) -> str:
    """Any slug is a valid agent_type; this checks shape, not membership.

    protocol/PROTOCOL.md#7-taxonomy-open-not-closed defines the taxonomy as
    open, trace.schema.json/lesson.schema.json declare agent_type as a plain
    string with no enum, and the Hub column is free text. The CLI was the one
    surface that disagreed: `choices=paths.AGENT_TYPES` on init/lesson new/
    import made a robotics or legal fleet impossible to declare, so both had
    to be filed under `custom` -- losing the distinction the field exists to
    record, in a product whose whole claim is that it works for any fleet.

    Rejecting by shape is still worth doing: the value is written unquoted
    into memory/INDEX.md's first line, reaches paths.store_agent_type's
    parser, and is stored in the Hub's String(64) column, so a value with a
    newline, a path separator, or 200 characters in it breaks something
    later and further away than here.
    """
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
    """`distill.find_clusters` treats `similarity_threshold <= 0` as a
    deliberate, documented degenerate mode -- "cluster everything into one"
    -- and that library behavior stays exactly as it is (tests/test_distill.py
    calls it directly with `similarity_threshold=0`). But `find_clusters`
    only performs that merge cheaply; the CANDIDATE DESCRIPTION step after
    it (`representative()`, an O(k^2) medoid search over whatever cluster
    resulted) then has to run over the WHOLE store as one cluster of size k,
    not the small near-duplicate groups it is sized for. `--similarity-
    threshold` reaches `taxonomy.build_taxonomy`/`find_clusters` from an
    argparse CLI flag on `distill`, `taxonomy`, and `pilot` alike, AND,
    unvalidated, from the customer-facing MCP tool `propose_lessons`'s
    `similarity` parameter (`mcp_server.py`, routed through distill_cmd's
    parser so the two surfaces can't drift) -- an agent or operator passing
    `similarity=0` on a store of a few thousand traces would trigger a
    several-million-comparison medoid computation in one call, with no
    warning that 0 means something qualitatively different from "loose
    matching" rather than "looser". Rejecting non-positive and above-1
    values here (Jaccard similarity is never outside [0, 1], so nothing in
    that range is even meaningful) is scoped to these argument-parsing
    entry points, not the library function real callers may still use
    directly.
    """
    try:
        threshold = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a number, got {value!r}") from None
    if not (0 < threshold <= 1):
        raise argparse.ArgumentTypeError(
            f"must be > 0 and <= 1 (Jaccard similarity's own range), got {threshold}"
        )
    return threshold


# Warn/refuse thresholds for free-text writes. A 5-10 MB pasted log would
# otherwise dominate the token cache JSON and the semantic index build, and
# ranking cost is linear in tokens. The schemas deliberately carry no
# maxLength (a protocol-breaking change that would invalidate existing
# lessons), so this CLI-level guard is additive: old lessons keep
# validating, only new oversized writes warn or refuse.
WARN_CHARS = 256 * 1024
REFUSE_CHARS = 1024 * 1024


def check_text_size(fields: dict[str, str], *, what: str) -> bool:
    """Warn (return True) or refuse (return False) an oversized write.

    `fields` maps field name -> text. Refuses when the total exceeds
    REFUSE_CHARS; warns when it exceeds WARN_CHARS. Messages go to stderr.
    """
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
