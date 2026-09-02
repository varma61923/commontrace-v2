"""What a lesson SAID -- content identity for the thing an experiment measured.

WHY THIS EXISTS
---------------
`commontrace/integrity.py` checks whether the sample an effect was computed
on can support it: attrition, arm balance, a salt that changed mid-run. It
established that a randomized comparison is only as good as its assignment
record.

It did not check the other half. A randomized comparison is also only as
good as its TREATMENT holding still, and nothing here held it still. A
lesson is a file. `commontrace lesson edit`, `draft_lesson` over MCP, and a
text editor all rewrite it in place, and the holdout log records the lesson
by SLUG -- a mutable name. So:

  - Edit a lesson on day 10 of a 30-day run and occasions 1-200 were treated
    with one rule, 201-400 with another. `analyze()` pools them into a single
    arm and reports an effect for a treatment that no longer exists. This is
    the same defect as the salt drift `integrity.check_assignment_drift`
    catches, one level down: there the RANDOMIZATION changed, here the THING
    BEING RANDOMIZED did.

  - Finish a run, report "lesson_x HELPS +12%, p=0.01", then rewrite
    lesson_x. The number in the renewal deck now describes text that is
    gone, and nothing anywhere records what it used to say.

Adding an MCP `draft_lesson` tool made this worse rather than better: before
it, rewriting a lesson mid-experiment took a person opening a file; now an
agent can do it mid-run, unattended, as a normal part of curating.

WHAT IS HASHED, AND WHY EXACTLY THIS
------------------------------------
The revision covers what an agent RECEIVES, and nothing else. That rule is
not fastidiousness, it is the only line that makes the check usable:

  `uses` and `last_hit` change on EVERY retrieval. Hashing them would make
  every lesson appear to change constantly, every experiment would be
  flagged, and the check would be pure noise inside a week -- the exact
  false positive that teaches people to ignore a validity report.

  `source_traces`, `source_episodes`, `importance_history` and
  `hub_trace_id` are provenance. They can be corrected long after the fact
  without changing one word an agent reads.

  `status` is lifecycle. review -> active is what makes a lesson injectable
  in the first place, so the revision that matters is the one it has while
  active; archiving it stops future injection but does not retroactively
  change what earlier occasions were treated with.

Whitespace is normalized before hashing. A reflowed paragraph is not a
different instruction, and flagging one as a changed treatment costs the
same credibility as missing a real change -- see `_normalize`.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# Exactly the frontmatter fields that reach an agent, in a fixed order so the
# digest is stable across dict orderings. Kept in step with
# `mcp_server._lesson_wire` by tests/test_revision.py, which is the surface
# that actually hands these to a model.
INJECTED_FIELDS = (
    "description",
    "domain",
    "tags",
    "importance",
    "applies_when",
    "do_not_apply_when",
)

# Length of the printed digest. Short enough to sit inside a report line
# (`lesson_x @ a3f9c1d2`), long enough that two revisions of one lesson
# colliding is not a thing that happens.
REVISION_LENGTH = 12

_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_RUN = re.compile(r"\n{3,}")


def _normalize(text: str) -> str:
    """Whitespace-insensitive, content-sensitive.

    A reflowed paragraph, a stripped trailing space, an extra blank line
    between sections: none of these change the instruction an agent acts on,
    and flagging them as a changed treatment would fill the validity report
    with findings nobody should act on. Anything that changes a WORD changes
    the digest.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_WS.sub("", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


def _canonical(fm: dict, body: str) -> str:
    payload: dict[str, Any] = {}
    for key in INJECTED_FIELDS:
        value = fm.get(key)
        if isinstance(value, list):
            # Order-insensitive: a reordered tag list is the same tag list,
            # and the ranker treats it as a set.
            payload[key] = sorted(str(v) for v in value)
        elif isinstance(value, str):
            payload[key] = _normalize(value)
        else:
            payload[key] = value
    payload["_body"] = _normalize(body or "")
    # sort_keys plus a fixed separator: the digest must not depend on how the
    # YAML happened to be ordered on disk, or a no-op rewrite would look like
    # a new revision.
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def revision_of(fm: dict, body: str = "") -> str:
    """Content identity for one lesson, as a short hex digest.

    Computed on read rather than stored in the file. A stored field would
    have to be updated by whatever wrote the lesson, which is six call sites
    today and a seventh the first time someone adds one -- and a revision
    that is merely *claimed* by the writer is not identity, it is a comment.
    Derived from the content, it cannot be wrong or go stale, it needs no
    migration, and it applies to every lesson that already exists.
    """
    digest = hashlib.sha256(_canonical(fm, body).encode("utf-8")).hexdigest()
    return digest[:REVISION_LENGTH]


def revision_of_trace(
    title: str, context_text: str, solution_text: str, tags: list[str] | None = None
) -> str:
    """Content identity for a Hub trace, under the same rules as a lesson.

    The Hub's holdout randomizes TRACES, and `amend_trace` rewrites a trace's
    title, context and solution in place -- so the Hub has the same defect
    the local tier had, on a different object. Same normalization and the
    same digest length, in this module rather than beside the Hub's models,
    because a revision that meant one thing on one tier and another on the
    other would make the two integrity reports incomparable.

    Excludes `outcome`. Recording how an occasion went is not a change to
    what an agent was shown -- and outcomes are attached AFTER retrieval by
    definition, so counting them would make every measured trace look edited.
    """
    payload = {
        "title": _normalize(title or ""),
        "context_text": _normalize(context_text or ""),
        "solution_text": _normalize(solution_text or ""),
        "tags": sorted(str(t) for t in (tags or [])),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:REVISION_LENGTH]


def same_treatment(a: tuple[dict, str], b: tuple[dict, str]) -> bool:
    """Would an agent read these two as the same instruction?"""
    return revision_of(*a) == revision_of(*b)
