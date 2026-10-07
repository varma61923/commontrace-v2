"""The local MCP server's tool surface, as names."""

from __future__ import annotations

LOCAL_TOOLS = (
    "retrieve", "capture", "propose_lessons", "list_lessons", "get_lesson",
    "draft_lesson", "approve_lesson", "reject_lesson", "store_status",
    "experiment_status",
    "memory_block_read", "memory_block_update", "memory_block_list", "memory_block_delete",
    "core_memory_append", "core_memory_replace",
    "archival_memory_insert", "archival_memory_search",
    "conversation_search",
    "query_facts", "record_fact", "fact_explain",
    "graph_query", "graph_neighbors", "graph_viz_html", "graph_timeline",
    "list_skills", "load_skill",
    "conversation_add", "conversation_recall", "conversation_profile", "conversation_forget",
    "conversation_summarize", "memory_recall",
    "lessons_from_trace", "traces_for_lesson", "community_members", "observation_evidence",
    "ingest_job_status", "ingest_documents_list", "ingest_document_get",
    "saga_create", "saga_get", "saga_list", "saga_append_event", "saga_update_brief",
    "knowledge_page_list", "knowledge_page_get", "knowledge_page_update",
    "session_ledger_record", "session_ledger_get", "session_ledger_summary",
    "procedural_memory_create", "procedural_memory_replay",
    "sql_guarded_query", "defense_screen_content",
)


APPROVAL_TOOLS = ("approve_lesson", "reject_lesson")
