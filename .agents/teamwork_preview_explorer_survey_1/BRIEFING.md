# BRIEFING — 2026-09-05T13:16:00Z

## Mission
Deep technical survey of `commontrace/` (CLI, subcommands, config, runner, attention integration) and `memory/` (attention mechanism, indexing, query, cosine similarity, lessons, episodes, `build_index.py`, `query.py`) across 4 requirements: Bugs & Edge Cases, Performance Bottlenecks, Security Vulnerabilities, Dead & Unwired Code.

## 🔒 My Identity
- Archetype: teamwork_preview_explorer
- Roles: Survey Explorer 1 (Core CLI & Memory/Attention)
- Working directory: /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_1
- Original parent: bf1be23f-c3e9-4aff-850c-99fca8f4e1d9
- Milestone: Survey Phase

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Scope limited to `commontrace/` and `memory/`
- Exhaustive catalog with file paths, line numbers, impact, remediation

## Current Parent
- Conversation ID: bf1be23f-c3e9-4aff-850c-99fca8f4e1d9
- Updated: 2026-09-05T13:02:02Z

## Investigation State
- **Explored paths**: `commontrace/` (all modules and subcommands), `memory/` (attention, lessons, episodes, index, reports)
- **Key findings**:
  1. Bugs & Edge Cases: Unhandled FileNotFoundError in `build_index.py` staleness check, unhandled OSError in `pilot_metrics.py:load_traces`, offset-naive datetime comparison bug in `measure_performance.py:compute_freshness`, ASCII-only `_TOKEN_RE` regex in `measure_performance.py`, `trace_io.py` regex lookahead swallowing extra sections into context_text, `evidence_io.load_active_lessons` failing to filter by active status, `taxonomy_cmd.py` unvalidated float threshold allowing `<=0` and `nan`, `cli.py` unhandled TypeError/IndexError.
  2. Performance Bottlenecks: `capture_cmd.py:find_trace_by_occasion` sequentially reading and YAML-parsing every trace file on disk on each capture, `pilot_cmd.py` quad-scanning trace directory, `query.py` double file reading (load_importances + check_staleness) on every query, `_shellout.py` spawning new Python subprocesses on every `query` and `index` command (~1.5-2.5s overhead), unvectorized pure-Python `estimate_jaccard` in `overlap.py`, probe file creation/unlink in `frontmatter._new_file_mode`.
  3. Security Vulnerabilities: SafeLoader-based YAML parsing verified across codebase; `allow_pickle=False` in npz loading verified; model name restricted to trusted constant in `query.py`; no `shell=True` subprocesses; path traversal protected in `lesson_io.py`, `validate.py`, and `capture_cmd.py`.
  4. Dead and Unwired Code: Missing `--include-importance-floor` flag in `query_cmd.py` CLI; ignored `--agent-type` flag in semantic query; help text in `serve_cmd.py` pointing to nonexistent `--mcp` flag; missing `memory/attention` scaffolding in `init_cmd.py`.
- **Unexplored areas**: None within scope. Complete survey achieved.

## Key Decisions Made
- Cataloged findings into 5-part Handoff Report with concrete file paths, line numbers, impact, and remediation.

## Artifact Index
- /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_1/DISPATCH.md — Assignment and parent check-in record
- /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_1/BRIEFING.md — Persistent memory
- /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_1/progress.md — Liveness heartbeat
- /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_1/handoff.md — Final handoff report
