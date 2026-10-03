"""The local MCP server's tool surface, as names."""

from __future__ import annotations

LOCAL_TOOLS = (
    "retrieve", "capture", "propose_lessons", "list_lessons", "get_lesson",
    "draft_lesson", "approve_lesson", "reject_lesson", "store_status",
    "experiment_status",
    "memory_block_read", "memory_block_update", "memory_block_list", "memory_block_delete",
    "query_facts", "record_fact",
    "graph_query", "graph_neighbors", "graph_viz_html",
    "list_skills", "load_skill",
    "conversation_add", "conversation_recall", "conversation_profile",
)

APPROVAL_TOOLS = ("approve_lesson", "reject_lesson")
