"""Shared rendering helpers for the listing commands."""
from __future__ import annotations

import os
import sys

from commontrace import frontmatter


def resolve_hub(args) -> tuple[str, str] | None:
    hub_url = args.hub_url or os.environ.get("COMMONTRACE_HUB_URL")
    api_key = args.hub_api_key or os.environ.get("COMMONTRACE_HUB_API_KEY")
    if args.hub_api_key:
        print(
            "[commontrace] [WARN] --hub-api-key was passed on the command line, which is "
            "visible to other local users (`ps`, /proc, shell history). Prefer setting "
            "COMMONTRACE_HUB_API_KEY instead.",
            file=sys.stderr,
        )
    if not hub_url or not api_key:
        print(
            "[commontrace] a Hub URL and API key are required.\n"
            "  Set COMMONTRACE_HUB_URL and COMMONTRACE_HUB_API_KEY, or pass\n"
            "  --hub-url / --hub-api-key.",
            file=sys.stderr,
        )
        return None
    return hub_url, api_key


def read_or_warn(read_fn, path: str):
    try:
        return read_fn(path)
    except (frontmatter.FrontmatterError, OSError, UnicodeDecodeError) as exc:
        print(f"[commontrace] warning: skipping unreadable file {path}: {exc}", file=sys.stderr)
        return None


def cell(value: object, placeholder: str = "?") -> str:
    """Render one frontmatter value for a fixed-width listing column."""
    return placeholder if value is None or value == "" else str(value)
