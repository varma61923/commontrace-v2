"""Randomized-holdout arm assignment and its append-only log.

Extracted so there is exactly ONE implementation. Two retrievers now apply
the holdout -- `commontrace query` (a person or a shell-capable agent) and
the local MCP server (an agent with no shell) -- and a second copy of a
randomized assignment is a second chance to bias the causal number the whole
experiment exists to produce.

The log records *eligibility*: every lesson written here matched the task and
was then either injected or deliberately withheld. That distinction is what
makes the later comparison causal rather than confounded, so it is written at
decision time and never reconstructed.
"""
from __future__ import annotations

import json
import os

from commontrace import experiment, frontmatter, paths

# Arm assignment is a deterministic hash of (lesson, occasion, SALT), so the
# salt is not cosmetic: two retrievers using different salts put the SAME
# lesson on the SAME occasion into DIFFERENT arms, and the analysis then joins
# a control-arm assignment to a treated outcome. It lives here, next to the
# assignment it parameterises, so `commontrace query` and the MCP retriever
# cannot drift apart by editing one default.
DEFAULT_SALT = "default"


def holdout_log_path(root: str) -> str:
    """Append-only record of every holdout assignment a retriever made.

    Persisted rather than recomputed because the analysis needs to know a
    lesson was *eligible* on an occasion -- that it matched the activation
    condition and was then either injected or withheld. Recomputing
    eligibility later would silently change it as the corpus changes.
    """
    return os.path.join(paths.memory_dir(root), "holdout_log.jsonl")


def assign_and_log(
    root: str,
    slugs: list[str],
    *,
    occasion_id: str,
    rate: float,
    salt: str,
) -> set[str]:
    """Decide which of `slugs` to withhold on this occasion, and record it.

    Assignment is a deterministic hash of (lesson, occasion, salt), so a
    retry returns the same answer and an occasion cannot change arms.
    """
    withheld = {s for s in slugs if experiment.is_held_out(s, occasion_id, rate, salt)}

    path = holdout_log_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Locked, and flushed inside the lock. O_APPEND makes a single write()
    # atomic, but Python buffers: a fleet whose agents retrieve concurrently
    # writes more than one buffer's worth, and a flush boundary can land
    # mid-line. The corrupted line is then dropped when the log is read --
    # and A DROPPED OBSERVATION IS NOT NEUTRAL. It removes one arm's data
    # point from a randomized comparison, biasing the result. Cheap to
    # prevent, near-impossible to detect after the fact.
    with frontmatter.locked(path):
        with open(path, "a", encoding="utf-8") as fh:
            for slug in slugs:
                fh.write(json.dumps({
                    "occasion_id": occasion_id,
                    "lesson": slug,
                    "injected": slug not in withheld,
                    "rate": rate,
                    "salt": salt,
                }) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return withheld
