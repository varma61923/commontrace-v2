"""Shared rendering helpers for the listing commands."""
from __future__ import annotations


def cell(value: object, placeholder: str = "?") -> str:
    """Render one frontmatter value for a fixed-width listing column.

    `dict.get(key, default)` returns the default only when the key is
    ABSENT. A key that is present with an empty YAML value (`status:`) parses
    to None, and None has no `__format__`, so a width spec like `:8s` raises
    TypeError. A half-finished hand edit is exactly when someone runs a
    listing to find the file that needs fixing, so the listing must survive
    it -- `lesson validate` already reports such a file cleanly, and the two
    commands disagreeing is the actual defect.

    Absent and present-but-empty are treated identically: both mean "nothing
    usable here".
    """
    return placeholder if value is None or value == "" else str(value)
