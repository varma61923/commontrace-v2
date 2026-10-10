# MCP and gateway API reference

Generated from statically decorated tool definitions. Each source link includes full parameters, defaults,
validation and return documentation. Optional tools depend on deployment capabilities/configuration.
CLI commands expose `--help`; Python APIs can be browsed with `python -m pydoc commontrace`.

Local MCP configuration and Hub bearer authentication examples are in [README](../README.md)
and [Hub README](../hub/README.md). HTTP/JSON-line gateway contracts remain versioned `/v1`.

Batch tools accept 1–25 items and return ordered `results`; commits are independent.
Use per-contribution idempotency keys. Batch delete is permanent; do not replay it automatically.

## commontrace/mcp_server.py

| Tool | Parameters | Description / source |
| --- | --- | --- |
| `retrieve` | `task, top_k, occasion_id, agent_type, exclude_shown, scope, as_of` | Find the lessons that apply to the task you are about to attempt. [source](../commontrace/mcp_server.py#L586) |
| `capture` | `title, context_text, solution_text, tags, agent_type, agent_id, occasion_id, resolved, escalated, repeated_error, frustration_signal, tokens_used, llm_calls, baseline` | Record what you just did, so it can become a lesson later. [source](../commontrace/mcp_server.py#L986) |
| `propose_lessons` | `min_cluster, similarity, extract, min_validation_score, failed_only, semantic_dedup` | Find repeated failures in what you have captured, and draft a [source](../commontrace/mcp_server.py#L1071) |
| `list_lessons` | `status, limit, offset` | List lightweight lesson summaries, newest first. [source](../commontrace/mcp_server.py#L1130) |
| `get_lesson` | `slug` | One lesson in full -- frontmatter and every body section. [source](../commontrace/mcp_server.py#L1166) |
| `draft_lesson` | `slug, rule, why, how_to_apply, counter_examples, applies_when, do_not_apply_when, description, importance, importance_rationale, tags, domain` | Write a candidate lesson's content. This is the curation step. [source](../commontrace/mcp_server.py#L1187) |
| `experiment_status` | `none` | Is the randomized holdout you are feeding actually going to answer? [source](../commontrace/mcp_server.py#L1413) |
| `store_status` | `none` | What this store holds, and where its gaps are. [source](../commontrace/mcp_server.py#L1495) |
| `memory_block_read` | `name` | Read a stateful working memory block (such as persona, human, or project). [source](../commontrace/mcp_server.py#L1532) |
| `memory_block_update` | `name, content, mode, old_content, line_number, expected_revision` | Update or append to a stateful working memory block with quota checking. [source](../commontrace/mcp_server.py#L1546) |
| `memory_block_list` | `none` | List all active working memory blocks currently configured in this store. [source](../commontrace/mcp_server.py#L1588) |
| `memory_block_delete` | `name, expected_revision` | Delete an existing working memory block. [source](../commontrace/mcp_server.py#L1598) |
| `core_memory_append` | `name, content, expected_revision` | Append to a core memory block (persona, human, or project). [source](../commontrace/mcp_server.py#L1617) |
| `core_memory_replace` | `name, old_content, new_content, expected_revision` | Replace one exact substring of a core memory block. [source](../commontrace/mcp_server.py#L1636) |
| `archival_memory_insert` | `content, category, scope` | Insert one passage into long-term archival memory. [source](../commontrace/mcp_server.py#L1658) |
| `archival_memory_search` | `query, scope, limit, scorer` | Search long-term archival memory for passages matching `query`. [source](../commontrace/mcp_server.py#L1678) |
| `conversation_search` | `question, space, budget, ctx` | Search past conversation turns for what answers `question`. [source](../commontrace/mcp_server.py#L1702) |
| `query_facts` | `query, scope, category, as_of, limit, show_expired, scorer` | Search distilled atomic facts with bitemporal validity and scoped routing. [source](../commontrace/mcp_server.py#L1765) |
| `fact_explain` | `fact_id, budget, max_sources, max_depth, as_of, scope` | Read bounded source quotations for an atomic fact, including attested [source](../commontrace/mcp_server.py#L1809) |
| `record_fact` | `statement, category, scope, confidence, evidence, min_support` | Record an atomic fact discovered during execution or reinforce an existing fact. [source](../commontrace/mcp_server.py#L1828) |
| `graph_query` | `entity, hops, as_of, known_at` | Explore entity relationships and multi-hop connected concepts in the knowledge graph. [source](../commontrace/mcp_server.py#L1862) |
| `graph_neighbors` | `entity, direction, relation, as_of` | Inspect the immediate direct neighbors of an entity in the causal knowledge graph. [source](../commontrace/mcp_server.py#L1884) |
| `graph_viz_html` | `as_of` | Render the knowledge graph as a self-contained interactive HTML page. [source](../commontrace/mcp_server.py#L1904) |
| `conversation_add` | `space, session, messages, session_at, ctx` | Remember messages from a conversation, in order, under a space (one user, agent or thread). [source](../commontrace/mcp_server.py#L1930) |
| `conversation_recall` | `space, question, budget, now, sessions, speakers, since, until, ctx, context_strategy, adaptive_budget` | What was said that answers `question`: the matching turns with their neighbours, [source](../commontrace/mcp_server.py#L1959) |
| `memory_recall` | `question, budget, agent, as_of, channels, spaces, evidence_budget, scope, ctx, fact_scorer, reranker, adaptive_budget` | One context from every kind of memory: approved lessons, atomic facts, graph [source](../commontrace/mcp_server.py#L1999) |
| `conversation_profile` | `space, history` | What the user has said about themselves in a space (preferences, identity, plans, [source](../commontrace/mcp_server.py#L2034) |
| `conversation_forget` | `space, session, before, expired` | Delete from a space: one `session`, messages said `before` a date, and/or messages [source](../commontrace/mcp_server.py#L2050) |
| `conversation_summarize` | `space, session, ctx` | Write an extractive summary (the most central dated sentences) for each session [source](../commontrace/mcp_server.py#L2069) |
| `graph_timeline` | `entity` | How an entity's relations changed over time: each edge that began or ended, when [source](../commontrace/mcp_server.py#L2089) |
| `lessons_from_trace` | `trace_id` | Lessons that cite `trace_id` in their `source_traces` (trace -> lesson lookup). [source](../commontrace/mcp_server.py#L2100) |
| `traces_for_lesson` | `slug` | The source trace ids a lesson cites in its frontmatter (lesson -> trace provenance lookup). [source](../commontrace/mcp_server.py#L2122) |
| `community_members` | `name` | Members of one topic community by name (from `commontrace community build`). [source](../commontrace/mcp_server.py#L2135) |
| `observation_evidence` | `id` | One consolidated observation with its cited evidence (quote + source_id). [source](../commontrace/mcp_server.py#L2154) |
| `list_skills` | `none` | List the reusable procedures (skills) available in this project, by name and description. [source](../commontrace/mcp_server.py#L2168) |
| `load_skill` | `name` | Load the full instructions of one skill named by `list_skills`. [source](../commontrace/mcp_server.py#L2182) |
| `ingest_job_status` | `job_id` | Inspect the current stage and progress of an ingestion job. [source](../commontrace/mcp_server.py#L2203) |
| `ingest_documents_list` | `query, limit` | List lightweight document summaries in the catalog (NOT dumping full contents). [source](../commontrace/mcp_server.py#L2216) |
| `ingest_document_get` | `doc_id_or_path, chunk_index` | Retrieve a registered document snapshot or chunk by ID or registered source path. [source](../commontrace/mcp_server.py#L2228) |
| `saga_create` | `saga_id, title, tags, brief, status` | Create an ordered incident or migration narrative saga with a [source](../commontrace/mcp_server.py#L2245) |
| `saga_get` | `saga_id` | Fetch a saga narrative along with its complete chronological event timeline, [source](../commontrace/mcp_server.py#L2264) |
| `saga_list` | `status, tag, limit` | List all recorded incident or migration sagas, optionally filtered [source](../commontrace/mcp_server.py#L2276) |
| `saga_append_event` | `saga_id, title, description, actor, new_brief, watermark` | Append a milestone, telemetry update, or incident response event to [source](../commontrace/mcp_server.py#L2286) |
| `saga_update_brief` | `saga_id, brief, watermark` | Update the rolling synthesis running brief of an ongoing saga narrative, [source](../commontrace/mcp_server.py#L2310) |
| `knowledge_page_list` | `tag, limit` | List all curated knowledge pages and mental model synthesis documents, [source](../commontrace/mcp_server.py#L2325) |
| `knowledge_page_get` | `slug, version` | Fetch the latest or a specific historical revision of a curated knowledge page, [source](../commontrace/mcp_server.py#L2335) |
| `knowledge_page_update` | `slug, content, title, tags, expected_version, dry_run, comment` | Update or create a curated knowledge page with dry-run diff preview. [source](../commontrace/mcp_server.py#L2347) |
| `session_ledger_record` | `session_id, model, prompt_tokens, completion_tokens, provider, cost_usd, occasion` | Record an LLM model call with prompt tokens, completion tokens, provider, [source](../commontrace/mcp_server.py#L2380) |
| `session_ledger_get` | `session_id` | Get aggregate token usage and estimated costs for a session with per-model breakdown. [source](../commontrace/mcp_server.py#L2401) |
| `session_ledger_summary` | `since, until` | Get global token usage and cost expenditure aggregated across all sessions, [source](../commontrace/mcp_server.py#L2409) |
| `procedural_memory_create` | `task_objective, progress_status, steps, agent_id, metadata` | Create and persist a structured procedural memory trajectory recording sequential [source](../commontrace/mcp_server.py#L2419) |
| `procedural_memory_replay` | `memory_id, token_budget` | Replay a procedural memory trajectory rendered into a structured markdown prompt context [source](../commontrace/mcp_server.py#L2458) |
| `sql_guarded_query` | `db_path, sql, max_rows, timeout_seconds` | Validate, cap, and safely execute a read-only SELECT query against a SQLite database with guardrails. [source](../commontrace/mcp_server.py#L2474) |
| `defense_screen_content` | `content, action` | Screen content against known sensitive data, credentials, PII, and injection patterns [source](../commontrace/mcp_server.py#L2500) |
| `approve_lesson` | `slug, rationale, approved_by` | Activate a reviewed lesson so retrieval starts injecting it. [source](../commontrace/mcp_server.py#L1270) |
| `reject_lesson` | `slug, reason` | Archive a candidate that should not become a lesson. `reason` is [source](../commontrace/mcp_server.py#L1390) |

## hub/server.py

| Tool | Parameters | Description / source |
| --- | --- | --- |
| `search_traces` | `query, tags, limit, offset, occasion_id, brief, pinned, scope, as_of` | Search this org's traces by full-text query and/or tags. [source](../hub/server.py#L590) |
| `contribute_trace` | `title, context_text, solution_text, tags, agent_type, agent_id, profile, outcome, idempotency_key, scopes, valid_from, valid_until` | Contribute a new trace. Returns its id, quarantine status, and [source](../hub/server.py#L678) |
| `get_trace` | `id` | Fetch a single trace by id. Not found (including a trace id that [source](../hub/server.py#L758) |
| `vote_trace` | `id, vote, feedback_tag, feedback_text` | Cast (or update) this org's vote ('up'/'down') on a trace: your [source](../hub/server.py#L772) |
| `amend_trace` | `id, title, context_text, solution_text, tags, outcome, idempotency_key` | Create a new trace that supersedes `id`, carrying forward any [source](../hub/server.py#L794) |
| `list_tags` | `none` | List every distinct tag used across this org's non-quarantined traces. [source](../hub/server.py#L846) |
| `fleet_outcomes` | `agent_type` | Has your fleet's agent performance changed since your baseline [source](../hub/server.py#L857) |
| `working_set` | `budget_chars` | Your fleet's proven memory, small enough to pin to a system prompt. [source](../hub/server.py#L901) |
| `value_delivered` | `value_per_occasion, rate_tiers` | What your fleet's memory has been worth, causally, in occasions. [source](../hub/server.py#L948) |
| `holdout_assign` | `trace_ids, occasion_id, pinned` | Randomized holdout: for each trace eligible on this occasion, [source](../hub/server.py#L1013) |
| `record_occasion_outcome` | `occasion_id, succeeded` | Report how an occasion went, closing the loop on every holdout [source](../hub/server.py#L1055) |
| `search_trace_content` | `pattern, regex, limit` | Locate traces (INCLUDING quarantined ones) whose title, context, [source](../hub/server.py#L1074) |
| `tag_trace_subjects` | `id, subject_ids` | Set (REPLACING any previous tags, not appending) which end [source](../hub/server.py#L1102) |
| `find_traces_by_subject` | `subject_id` | Every trace THIS ORG explicitly tagged (`tag_trace_subjects`) [source](../hub/server.py#L1128) |
| `purge_traces_by_subject` | `subject_id` | Permanently delete every trace this org tagged with [source](../hub/server.py#L1143) |
| `delete_trace` | `id` | Permanently delete one of your own traces, and every trace in [source](../hub/server.py#L1161) |
| `request_account_deletion` | `none` | Start permanently deleting YOUR ENTIRE ORGANIZATION -- every [source](../hub/server.py#L1178) |
| `cancel_account_deletion` | `none` | Cancel a pending request_account_deletion request. Needs no [source](../hub/server.py#L1197) |
| `confirm_account_deletion` | `confirmation_token` | The second call: permanently deletes this organization and [source](../hub/server.py#L1210) |
| `add_comment` | `trace_id, body` | Leave a remark on one of your org's own traces, visible to your [source](../hub/server.py#L1387) |
| `list_comments` | `trace_id` | Every comment left on one of your org's own traces, oldest first. [source](../hub/server.py#L1403) |
| `assign_trace` | `trace_id, user_id` | Make `user_id` (one of your org's own `hub.manage list-users` [source](../hub/server.py#L1413) |
| `unassign_trace` | `trace_id` | Clear whoever this trace is currently assigned to, if anyone. [source](../hub/server.py#L1427) |
| `list_my_notifications` | `unread_only` | Your own inbox: 'you were assigned a trace' / 'someone commented [source](../hub/server.py#L1440) |
| `mark_notification_read` | `notification_id` | Mark one of YOUR OWN inbox entries read. Never affects, or even [source](../hub/server.py#L1457) |
| `account_usage` | `none` | What your plan entitles you to, and what you have used this period. [source](../hub/server.py#L1477) |
| `contribute_traces_batch` | `traces` | Contribute 1–25 traces, with ordered per-item outcomes and independent commits. [source](../hub/server.py#L1494) |
| `get_traces_batch` | `ids` | Read 1–25 traces in order using each item's existing privacy policy. [source](../hub/server.py#L1503) |
| `delete_traces_batch` | `ids` | Permanently delete 1–25 owned trace chains; operations commit independently. [source](../hub/server.py#L1508) |
| `commons_overlap` | `failures, threshold, include_matches, agent_type` | Of the recurring failures your fleet keeps hitting, what fraction [source](../hub/server.py#L1235) |
| `commons_search` | `query_signature, limit, agent_type` | Ask the CommonTrace Knowledge Base what it already knows about [source](../hub/server.py#L1265) |
| `commons_export` | `limit` | Fetch the whole curated Knowledge Base corpus, to match against [source](../hub/server.py#L1302) |
| `submit_kb_entry` | `title, context_text, solution_text, tags, agent_type, rationale, idempotency_key` | Propose an entry for the CommonTrace Knowledge Base -- like [source](../hub/server.py#L1333) |
| `list_my_kb_submissions` | `limit` | Your org's own Knowledge Base submissions and their review [source](../hub/server.py#L1372) |

## Gateway HTTP routes

| Method | Route |
| --- | --- |
