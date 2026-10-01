"""The gates a drafted lesson must pass before it can be activated, as one function.

`commontrace lesson approve` refuses a candidate for unedited scaffolding, a high-confidence secret or
injection finding, or restating an active lesson. The draft-quality harness needs the same three
questions answered without activating anything, so they are asked here, in the same order and with the
same functions. A test (tests/test_draft_quality.py) holds this and `lesson approve` to the same
verdict on every case, so the two cannot drift apart.
"""
from __future__ import annotations

from commontrace import memory_guard, redundancy, templates
from commontrace.commands.lesson_cmd import _guard_fields


def gate_failures(fm: dict, body: str, active_texts: list[tuple[str, str]]) -> list[str]:
    """Names of the approval gates this lesson fails: "scaffolding", "safety", "redundancy".
    Empty means it would be approved as written. `active_texts` is `(slug, comparable text)` for each
    active lesson, as `lesson approve` builds it."""
    failed = []
    if templates.unfilled_placeholders(fm, body):
        failed.append("scaffolding")
    if memory_guard.scan_fields(_guard_fields(fm, body)).should_block:
        failed.append("safety")
    if redundancy.closest(redundancy.comparable_text(fm, body), active_texts,
                          threshold=redundancy.DEFAULT_THRESHOLD) is not None:
        failed.append("redundancy")
    return failed
