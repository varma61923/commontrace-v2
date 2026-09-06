"""Shared argparse `type=` validators for CLI flags reused across commands."""
from __future__ import annotations

import argparse


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
