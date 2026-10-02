"""The gates a drafted lesson must pass before it can be activated, as one function."""
from __future__ import annotations

from commontrace import memory_guard, redundancy, templates
from commontrace.commands.lesson_cmd import _guard_fields


def gate_failures(fm: dict, body: str, active_texts: list[tuple[str, str]]) -> list[str]:
    failed = []
    if templates.unfilled_placeholders(fm, body):
        failed.append("scaffolding")
    if memory_guard.scan_fields(_guard_fields(fm, body)).should_block:
        failed.append("safety")
    if redundancy.closest(redundancy.comparable_text(fm, body), active_texts,
                          threshold=redundancy.DEFAULT_THRESHOLD) is not None:
        failed.append("redundancy")
    return failed
