"""Shared trace loaders for taxonomy_cmd / impact_cmd / pilot_cmd."""
from __future__ import annotations

import glob
import os
import sys

from commontrace import distill, paths, trace_io
from commontrace.frontmatter import FrontmatterError


def _safe_tags(raw: object) -> list[str]:
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
