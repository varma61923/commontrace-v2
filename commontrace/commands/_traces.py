"""Shared trace loaders for taxonomy_cmd / impact_cmd / pilot_cmd.

Deliberately separate from distill_cmd's own `_load_traces` (which returns
`distill.TraceCandidate` for clustering) even though the shapes overlap --
that function is exercised by distill's existing tests and this module adds
a second loader (`load_trace_instances`) that needs the full raw instance
(outcome, extensions, baseline flag) rather than the clustering-only
subset. Two small loaders that read the same directory is a cheaper risk
than reshaping a tested command's internals for callers that did not need
that change.
"""
from __future__ import annotations

import glob
import os
import sys

from commontrace import distill, paths, trace_io
from commontrace.frontmatter import FrontmatterError


def _safe_tags(raw: object) -> list[str]:
    """See distill_cmd._safe_tags -- same malformed-YAML-scalar guard."""
    return [str(t) for t in raw if t is not None] if isinstance(raw, (list, tuple)) else []


def _iter_trace_paths(root: str):
    tdir = paths.traces_dir(root)
    for p in sorted(glob.glob(os.path.join(tdir, "*.md"))):
        if os.path.basename(p) == "README.md":
            continue
        yield p


def load_trace_candidates(root: str, agent_type: str | None = None) -> list[distill.TraceCandidate]:
    """Traces shaped for distill.find_clusters -- used by `commontrace taxonomy`."""
    out = []
    for path in _iter_trace_paths(root):
        try:
            instance, _ = trace_io.read(path)
        except FrontmatterError as exc:
            print(f"[commontrace] warning: skipping unreadable trace {path}: {exc}", file=sys.stderr)
            continue
        if agent_type and instance.get("agent_type") != agent_type:
            continue
        if not instance.get("id"):
            continue
        out.append(
            distill.TraceCandidate(
                id=instance["id"],
                path=path,
                title=instance.get("title", ""),
                context_text=instance.get("context_text", ""),
                solution_text=instance.get("solution_text", ""),
                tags=_safe_tags(instance.get("tags")),
                agent_type=instance.get("agent_type", ""),
            )
        )
    return out


def load_trace_instances(root: str, agent_type: str | None = None) -> list[dict]:
    """Full raw trace instances (outcome, extensions, baseline flag) -- used
    by `commontrace impact` and `commontrace pilot`."""
    out = []
    for path in _iter_trace_paths(root):
        try:
            instance, _ = trace_io.read(path)
        except FrontmatterError as exc:
            print(f"[commontrace] warning: skipping unreadable trace {path}: {exc}", file=sys.stderr)
            continue
        if agent_type and instance.get("agent_type") != agent_type:
            continue
        out.append(instance)
    return out
