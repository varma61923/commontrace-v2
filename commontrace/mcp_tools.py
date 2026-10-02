"""The local MCP server's tool surface, as names."""

from __future__ import annotations

LOCAL_TOOLS = (
    "retrieve", "capture", "propose_lessons", "list_lessons", "get_lesson",
    "draft_lesson", "approve_lesson", "reject_lesson", "store_status",
    "experiment_status",
)

APPROVAL_TOOLS = ("approve_lesson", "reject_lesson")
