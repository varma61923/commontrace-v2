# Worker M1 (Gen 2) Dispatch: Core CLI, Trace/Evidence IO & Memory/Attention

## Assigned Scope & Files Owned
You exclusively own and modify:
- `memory/attention/build_index.py`
- `commontrace/reference/pilot_metrics.py` (load_traces error handling)
- `commontrace/trace_io.py`
- `commontrace/evidence_io.py`
- `commontrace/commands/taxonomy_cmd.py`
- `commontrace/cli.py`
- `commontrace/commands/capture_cmd.py` (both occasion probe & open-vocabulary --agent-type)
- `commontrace/commands/pilot_cmd.py`
- `memory/attention/query.py`
- `commontrace/overlap.py`
- `commontrace/frontmatter.py`
- `commontrace/commands/query_cmd.py`
- `commontrace/commands/serve_cmd.py`
- `commontrace/commands/init_cmd.py`
- `tests/test_m1_regressions.py` (dedicated new regression test file)

DO NOT modify files outside this set.

## Specific Tasks
1. `memory/attention/build_index.py:274-277`:
   Wrap `os.path.getmtime(p)` in a safe helper (`try...except OSError: return 0.0`) so concurrent file deletion/renaming during staleness check does not crash with `FileNotFoundError`/`OSError`.
2. `commontrace/reference/pilot_metrics.py:53-64`:
   In `load_traces`, wrap `open()` and `read()` in `try...except OSError: continue` so locked or unreadable files don't crash `load_traces`.
3. `commontrace/trace_io.py:19-22`:
   In `_SECTION_RE`, change positive lookahead from `(?=\n##\s*(?:Context|Solution)\s*\n|\Z)` to `(?=\n##\s|\Z)` so intermediate headings (like `## Notes`, `## Outcome`) are not swallowed into `context_text`.
4. `commontrace/evidence_io.py:27-38`:
   In `load_active_lessons`, ensure only active lessons are loaded: filter `if fm.get("status") == "active"` (defaulting to "active" if status key is missing, skipping "archived" and "review").
5. `commontrace/commands/taxonomy_cmd.py:21`:
   Validate `--similarity-threshold` using `_similarity_threshold` validator (must be `0 < threshold <= 1`).
6. `commontrace/cli.py:120-136`:
   Expand the exception tuple to catch `(OSError, ValueError, KeyError, csv.Error, TypeError, IndexError, AttributeError)`.
7. `commontrace/commands/capture_cmd.py`:
   - Optimize `_find_trace_by_occasion`: Probe `*_{_id_suffix(occasion_id)}.md` directly before falling back to full directory scan.
   - Remove `choices=paths.AGENT_TYPES` from `--agent-type` argument to support open-vocabulary agent types per Protocol §7, keeping default resolution intact.
8. `commontrace/commands/pilot_cmd.py:74-96`:
   Eliminate redundant trace directory walks by caching or reusing loaded traces in `run()`.
9. `memory/attention/query.py:102-146, 241-276`:
   Eliminate redundant dual-pass disk scans by recording mtimes during `load_importances` so `check_staleness` doesn't do a second full glob+stat pass.
10. `commontrace/commands/query_cmd.py:27`:
    Expose `--include-importance-floor` argument in `query_cmd.py` and forward it to `query.py`.
11. `commontrace/commands/serve_cmd.py:21`:
    Fix inaccurate help text from `commontrace install --mcp` to `commontrace install --target generic-mcp`.
12. `commontrace/commands/init_cmd.py:24-69`:
    Scaffold `memory/attention/` directory and ensure a clean starting point.
13. `commontrace/overlap.py:171-175`:
    Vectorize MinHash Jaccard comparison using numpy array equality when numpy is available.
14. `commontrace/frontmatter.py:178-185`:
    Avoid redundant probe file creation on every write if directory umask is already probed or can be determined safely.
15. Create dedicated regression tests in `tests/test_m1_regressions.py` that specifically test each of the above fixes!
16. Run `pytest tests/ -v` and verify all tests pass with 0 failures or regressions.

## 2026-09-05T15:05:24Z
You are Worker M1 Generation 2 (teamwork_preview_worker), replacing worker_m1 following a transient network disconnect.
Your working directory is: /root/commontrace-v2/.agents/worker_m1_gen2
The project workspace is: /root/commontrace-v2
