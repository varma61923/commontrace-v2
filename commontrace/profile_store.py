"""Static/dynamic profile split backed by memory/profile.md.

Sections delimited by HTML comments so both humans and tools can edit safely:

    # Profile
    <!-- static -->
    ...stable traits...
    <!-- /static -->
    <!-- dynamic -->
    - <iso> entry...
    <!-- /dynamic -->
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from commontrace import paths

STATIC_OPEN = "<!-- static -->"
STATIC_CLOSE = "<!-- /static -->"
DYNAMIC_OPEN = "<!-- dynamic -->"
DYNAMIC_CLOSE = "<!-- /dynamic -->"


def _profile_path(root: str) -> str:
    path = os.path.join(paths.memory_dir(root), "profile.md")
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    return path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _render(static: str, dynamic_lines: list[str]) -> str:
    body_dyn = "\n".join(dynamic_lines)
    if body_dyn:
        body_dyn += "\n"
    return (
        "# Profile\n\n"
        f"{STATIC_OPEN}\n{static.rstrip()}\n{STATIC_CLOSE}\n\n"
        f"{DYNAMIC_OPEN}\n{body_dyn}{DYNAMIC_CLOSE}\n"
    )


def _parse(text: str) -> tuple[str, list[str]]:
    def _section(open_m: str, close_m: str) -> str:
        pat = re.compile(re.escape(open_m) + r"(.*?)" + re.escape(close_m), re.DOTALL)
        m = pat.search(text)
        return m.group(1).strip() if m else ""

    static = _section(STATIC_OPEN, STATIC_CLOSE)
    dyn_raw = _section(DYNAMIC_OPEN, DYNAMIC_CLOSE)
    if not dyn_raw and STATIC_OPEN not in text and DYNAMIC_OPEN not in text:
        # Legacy file without markers: treat whole body (minus title) as static.
        lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        static = "\n".join(lines).strip()
        return static, []
    dynamic = [ln.rstrip() for ln in dyn_raw.splitlines() if ln.strip()]
    return static, dynamic


def load_profile(root: str) -> dict[str, object]:
    """Return {"static": str, "dynamic": list[str]}; missing file -> empty."""
    path = _profile_path(root)
    if not os.path.exists(path):
        return {"static": "", "dynamic": []}
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    static, dynamic = _parse(text)
    return {"static": static, "dynamic": dynamic}


def save_profile_static(root: str, text: str) -> dict[str, object]:
    """Replace the static section, preserving dynamic entries. Returns profile."""
    current = load_profile(root)
    dynamic = list(current.get("dynamic", []))  # type: ignore[union-attr]
    rendered = _render(str(text or "").strip(), dynamic)
    path = _profile_path(root)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(rendered)
    os.replace(tmp, path)
    return {"static": str(text or "").strip(), "dynamic": dynamic}


def append_dynamic(root: str, entry: str, timestamp: str | None = None) -> dict[str, object]:
    """Append one timestamped dynamic entry. Returns updated profile."""
    entry = (entry or "").strip()
    if not entry:
        raise ValueError("dynamic entry cannot be empty")
    current = load_profile(root)
    static = str(current.get("static", ""))
    dynamic = list(current.get("dynamic", []))  # type: ignore[union-attr]
    stamp = timestamp or _now_iso()
    dynamic.append(f"- {stamp} {entry}")
    path = _profile_path(root)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(_render(static, dynamic))
    os.replace(tmp, path)
    return {"static": static, "dynamic": dynamic}


def prune_dynamic(root: str, keep: int = 20) -> dict[str, object]:
    """Keep only the last `keep` dynamic entries. Returns updated profile."""
    if keep < 0:
        raise ValueError("keep must be >= 0")
    current = load_profile(root)
    static = str(current.get("static", ""))
    dynamic = list(current.get("dynamic", []))  # type: ignore[union-attr]
    pruned = dynamic[-keep:] if keep else []
    path = _profile_path(root)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(_render(static, pruned))
    os.replace(tmp, path)
    return {"static": static, "dynamic": pruned}
