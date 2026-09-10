"""Why did that return nothing, and what is the next command?

A store can be empty-looking for four different reasons, and until this
module existed every one of them produced the same sentence:

    [commontrace] no lexical matches. Try `commontrace lesson list` for a
    full view.

That is correct only in the last of the four cases. Measured by walking a
cold start end to end, a new user who does the obvious thing --

    commontrace init
    commontrace capture --title ... --context ... --solution ...
    commontrace query "..."

-- gets that message, then gets it again after `commontrace distill`
finds nothing, then gets it a THIRD time after writing a lesson by hand,
because a new lesson lands at `status=review` and `query` only ranks
ACTIVE ones. Three dead ends, one message, and none of them names the
actual blocker. The advice it does give ("try lesson list") shows an
empty list in the first two cases, which reads as "this product is
broken" rather than "you are three-quarters of the way through a
pipeline".

The pipeline is deliberate and worth keeping: traces are raw experience,
lessons are curated rules, and only a human (or an explicit approval)
promotes review -> active, because an unreviewed rule injected into every
future agent run is how a memory system starts doing harm. Nothing here
shortcuts that. What this does is tell the truth about WHERE the caller
is standing in it, and name the one command that moves them forward.

Read-only and never raises: a diagnostic that can fail is worse than no
diagnostic, because it fails at exactly the moment the user is already
confused.

That "never raises" is scoped deliberately narrow, because the first
version of this file was not. It wrapped the lesson scan in a bare
`except Exception: continue`, and `frontmatter.read` returns a
`(dict, body)` TUPLE rather than a dict -- so `fm.get("status")` raised
AttributeError on every lesson, the blanket handler swallowed it, and the
module cheerfully reported "no lessons yet" for a store that had them.
The catch-all defence hid a plain API misuse and produced confidently
wrong output, which is worse than the crash it was there to prevent. Each
handler below now names the failure it actually expects (`OSError`,
`FrontmatterError`); a TypeError or AttributeError here is a bug in this
file and should be loud.
"""
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
    """Count traces and lessons-by-status. Never raises.

    An unreadable or malformed lesson is skipped rather than reported: it
    is a real problem, but `commontrace doctor` is where a store's health
    belongs, and a retrieval miss is the wrong moment to raise a second,
    unrelated alarm.
    """
    try:
        traces = sum(
            1
            for path in glob.glob(os.path.join(paths.traces_dir(root), "*.md"))
            # `commontrace init` writes traces/README.md explaining the
            # directory. Counting it reported "1 trace" for a store nobody
            # had captured anything into yet -- the single most misleading
            # thing this module could say, since it turns "you are at the
            # start" into "something is wrong with what you captured".
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
        # Scaffolding shipped by `init`, not something the user wrote. Left
        # in the count, "this store has no lessons" could never be true.
        if slug == "lesson_template":
            continue
        try:
            fm, _body = frontmatter.read(path)
        except frontmatter.FrontmatterError:
            # A malformed lesson is a real problem, but `commontrace doctor`
            # is where store health belongs; a retrieval miss is the wrong
            # moment to raise a second, unrelated alarm.
            continue
        status = str(fm.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        if status == "review":
            review_slugs.append(slug)
    return StoreState(traces, by_status, review_slugs)


def why_no_results(root: str, *, searched: str = "query") -> str:
    """The one message that fits this store, ending in a runnable command.

    `searched` names the caller so the explanation reads correctly from
    `query` and from `serve`'s retrieve tool alike.
    """
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

    # Active lessons exist and genuinely none matched. Only here is "no
    # match" the honest answer rather than a stage of the pipeline.
    plural = "s" if state.active != 1 else ""
    return (
        f"[commontrace] no match among {state.active} active lesson{plural}.\n"
        "  See what is there:            commontrace lesson list\n"
        "  Loosen the relevance floor:   commontrace query '...' --relevance-floor 0.0"
    )
