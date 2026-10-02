"""Why did that return nothing, and what is the next command?"""
from __future__ import annotations

import glob
import os

from commontrace import frontmatter, paths


class StoreState:
    """Counts behind the four cases, plus the sentence that fits."""

    def __init__(self, traces: int, by_status: dict[str, int], review_slugs: list[str]):
        self.traces = traces
        self.by_status = by_status
        self.review_slugs = review_slugs

    @property
    def active(self) -> int:
        return self.by_status.get("active", 0)

    @property
    def review(self) -> int:
        return self.by_status.get("review", 0)

    @property
    def lessons(self) -> int:
        return sum(self.by_status.values())


def inspect(root: str) -> StoreState:
    """Count traces and lessons-by-status. Never raises."""
    try:
        traces = sum(
            1
            for path in glob.glob(os.path.join(paths.traces_dir(root), "*.md"))
            if os.path.basename(path).lower() != "readme.md"
        )
    except OSError:
        traces = 0

    by_status: dict[str, int] = {}
    review_slugs: list[str] = []
    try:
        paths_found = sorted(glob.glob(os.path.join(paths.lessons_dir(root), "lesson_*.md")))
    except OSError:
        paths_found = []
    for path in paths_found:
        slug = os.path.splitext(os.path.basename(path))[0]
        if slug == "lesson_template":
            continue
        try:
            fm, _body = frontmatter.read(path)
        except frontmatter.FrontmatterError:
            continue
        status = str(fm.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        if status == "review":
            review_slugs.append(slug)
    return StoreState(traces, by_status, review_slugs)


def why_no_results(root: str, *, searched: str = "query") -> str:
    """The one message that fits this store, ending in a runnable command."""
    state = inspect(root)

    if state.lessons == 0 and state.traces == 0:
        return (
            "[commontrace] this store is empty -- nothing has been captured yet.\n"
            "  Capture experience as it happens:\n"
            "    commontrace capture --title '...' --context '...' --solution '...'\n"
            "  Or write a rule directly, if you already know it:\n"
            "    commontrace lesson new --slug lesson_my_rule --description '...' --domain '...'"
        )

    if state.lessons == 0:
        plural = "s" if state.traces != 1 else ""
        return (
            f"[commontrace] {state.traces} trace{plural} captured, but no lessons yet -- and "
            f"`{searched}` ranks LESSONS, not traces.\n"
            "  Traces are raw experience; a lesson is the reusable rule distilled from them.\n"
            "  Propose lessons from repeated patterns (needs >= 2 similar traces):\n"
            "    commontrace distill\n"
            "  Or write the rule directly, which works with a single trace:\n"
            "    commontrace lesson new --slug lesson_my_rule --description '...' --domain '...'"
        )

    if state.active == 0:
        plural = "s" if state.review != 1 else ""
        listed = ", ".join(state.review_slugs[:3])
        more = f" (and {len(state.review_slugs) - 3} more)" if len(state.review_slugs) > 3 else ""
        return (
            f"[commontrace] {state.review} lesson{plural} at status=review, none active -- and "
            f"`{searched}` only returns ACTIVE lessons.\n"
            "  That gate is deliberate: an unreviewed rule injected into every future run is "
            "how memory starts doing harm.\n"
            f"  Fill in the Rule / Why / How-to-apply sections, then approve: {listed}{more}\n"
            f"    commontrace lesson approve {state.review_slugs[0] if state.review_slugs else '<slug>'}"
        )

    plural = "s" if state.active != 1 else ""
    return (
        f"[commontrace] no match among {state.active} active lesson{plural}.\n"
        "  See what is there:            commontrace lesson list\n"
        "  Loosen the relevance floor:   commontrace query '...' --relevance-floor 0.0"
    )
