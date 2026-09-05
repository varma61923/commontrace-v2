# CommonTrace Survey Report — Explorer 1 (Core CLI & Memory/Attention)

**Date**: 2026-09-05T13:20:00Z  
**Author**: Survey Explorer 1 (`teamwork_preview_explorer_survey_1`)  
**Scope**: `commontrace/` (CLI, subcommands, config, runner, attention integration) and `memory/` (attention mechanism, indexing, query, cosine similarity, lessons, episodes, `build_index.py`, `query.py`).

---

## 1. Observation

Direct observations from source inspection and execution in the workspace:

### 1.1 Bugs & Edge Cases (R1)

1. **`memory/attention/build_index.py:273-277` — Unhandled `FileNotFoundError` during Staleness Check**
   ```python
   273:         index_mtime = os.path.getmtime(INDEX_PATH)
   274:         newest_lesson = max(
   275:             (os.path.getmtime(p) for p in glob.glob(os.path.join(LESSONS_DIR, "lesson_*.md"))),
   276:             default=0.0,
   277:         )
   ```
   In `iter_active_lessons` (lines 149-160), file opens are guarded by `try...except OSError`. However, in lines 274-277, `os.path.getmtime(p)` is executed without exception handling inside the generator expression. If any lesson file is deleted or renamed between `glob.glob` and `os.path.getmtime`, `build_index.py` crashes with an unhandled `FileNotFoundError` / `OSError`.

2. **`commontrace/reference/pilot_metrics.py:53-64` — Unhandled `OSError` in `load_traces`**
   ```python
   53:     for p in sorted(glob.glob(os.path.join(tdir, "*.md"))):
   54:         if os.path.basename(p) == "README.md":
   55:             continue
   56:         with open(p, encoding="utf-8-sig") as fh:
   57:             fm = mp.parse_frontmatter(fh.read())
   ```
   `load_traces` iterates through files returned by `glob.glob` and opens each file without a `try...except OSError` block. If any trace file has restricted read permissions, is locked, or is deleted/renamed concurrently by another process, `pilot_metrics.py` (and therefore `commontrace bench --pilot`) crashes with an uncaught `OSError` / `PermissionError`.

3. **`commontrace/reference/measure_performance.py:984-1002` — Naive Datetime Comparison in `compute_freshness`**
   ```python
   984: def compute_freshness(lessons, now=None):
   ...
   994:     now = now or datetime.datetime.now()
   995:     cutoff = now - datetime.timedelta(days=FRESHNESS_WINDOW_DAYS)
   ...
   998:         hit = _parse_last_hit(fm.get("last_hit"))
   999:         if hit is not None and hit >= cutoff:
   ```
   `datetime.datetime.now()` produces an offset-naive datetime. `_parse_last_hit` produces naive datetimes by parsing `%Y-%m-%d`. If an external caller or test supplies a timezone-aware UTC datetime for `now` (the standard in `commontrace`), comparing `hit >= cutoff` raises:
   `TypeError: can't compare offset-naive and offset-aware datetimes`.

4. **`commontrace/reference/measure_performance.py:919-933` — Inconsistent ASCII-Only Regex in `_lexical_tokens`**
   ```python
   919: _TOKEN_RE = re.compile(r"[a-z0-9]+")
   ...
   930: def _lexical_tokens(text):
   931:     return {t for t in _TOKEN_RE.findall(str(text or "").lower())
   932:             if len(t) > 2 and t not in _LEXICAL_STOPWORDS}
   ```
   `measure_performance.py` defines an ASCII-only `_TOKEN_RE = re.compile(r"[a-z0-9]+")`. In `commontrace/_lexical.py`, this exact bug was previously identified and fixed using `_WORD_RE = re.compile(r"\w+", re.UNICODE)` because ASCII-only regex strips non-Latin text (accents, umlauts, CJK, Cyrillic). `measure_performance.py` still retains the buggy ASCII-only regex for `compute_lexical_duplicates`.

5. **`commontrace/trace_io.py:19-22` — Greedy Regex Swallowing Extra Sections into `context_text`**
   ```python
   19: _SECTION_RE = re.compile(
   20:     r"^##\s*(Context|Solution)\s*\n(.*?)(?=\n##\s*(?:Context|Solution)\s*\n|\Z)",
   21:     re.DOTALL | re.MULTILINE | re.IGNORECASE,
   22: )
   ```
   The positive lookahead `(?=\n##\s*(?:Context|Solution)\s*\n|\Z)` only terminates at `## Context` or `## Solution`. If a markdown body contains other valid headings (e.g., `## Outcome`, `## Notes`, `## Summary`) between `## Context` and `## Solution`, `_SECTION_RE` captures everything up to `## Solution` as `context_text`. Contrast with `commontrace/templates.py:152` which properly looks ahead to `(?=\n##\s|\Z)`.

6. **`commontrace/evidence_io.py:27-38` — `load_active_lessons` Does Not Filter by `status == "active"`**
   ```python
   27: def load_active_lessons(root: str) -> list[dict]:
   28:     out = []
   29:     for path in sorted(glob.glob(os.path.join(paths.lessons_dir(root), "lesson_*.md"))):
   30:         if os.path.basename(path) == "lesson_template.md":
   31:             continue
   32:         result = read_or_warn(frontmatter.read, path)
   33:         if result is None:
   34:             continue
   35:         fm, body = result
   36:         fm[BODY_KEY] = body
   37:         out.append(fm)
   38:     return out
   ```
   Despite its name, `load_active_lessons` returns ALL lessons regardless of status (`active`, `review`, or `archived`). Callers like `commands/taxonomy_cmd.py`, `commands/reliability_cmd.py`, `commands/pilot_cmd.py`, and `mcp_server.py:store_status` either have to defensively re-filter or risk treating archived/draft lessons as active.

7. **`commontrace/commands/taxonomy_cmd.py:21` — Unvalidated Float Threshold Parameter**
   ```python
   21:     p.add_argument("--similarity-threshold", type=float, default=0.3)
   ```
   `distill_cmd.py:13-42` defines `_similarity_threshold` which validates `0 < threshold <= 1`. In contrast, `taxonomy_cmd.py` accepts any float (such as `<= 0` or `nan`). Passing `0` or negative causes all traces to cluster into one massive group, while `nan` causes all comparisons to fail silently (`sim >= nan` is always false), producing zero clusters.

8. **`commontrace/cli.py:120-136` — Unhandled Exceptions Outside Specific Operational Tuple**
   ```python
   120:     except (OSError, ValueError, KeyError, csv.Error) as exc:
   ...
   135:         print(f"[commontrace] error: {type(exc).__name__}: {exc}", file=sys.stderr)
   136:         return 1
   ```
   Common unexpected runtime exceptions like `TypeError`, `IndexError`, or `AttributeError` from invalid inputs in subcommands are unhandled and bubble up to print raw internal interpreter tracebacks.

---

### 1.2 Performance Bottlenecks (R2)

1. **`commontrace/commands/capture_cmd.py:126-140` — Full Linear Trace Directory Scan on Every Capture**
   ```python
   126: def _find_trace_by_occasion(tdir: str, occasion_id: str) -> str | None:
   127:     for path in sorted(glob.glob(os.path.join(tdir, "*.md"))):
   128:         try:
   129:             fm, _ = frontmatter.read(path)
   ...
   137:         if str(fm.get("id", "")) == occasion_id:
   138:             return path
   ```
   Whenever `capture` is run with `--occasion-id` (including via MCP `capture`), `_find_trace_by_occasion` iterates through every trace in `memory/traces/*.md` and parses its YAML frontmatter. Traces are named `{date}_{slug}_{_id_suffix(trace_id)}.md`. Instead of directly testing `*_{_id_suffix(occasion_id)}.md` or indexing, it executes $O(N)$ full disk reads and YAML deserializations per capture call. In a repository with thousands of traces, this creates seconds of latency per task conclusion.

2. **`commontrace/commands/pilot_cmd.py:74-96` — Redundant Quad-Walk of Trace Directory**
   In `pilot_cmd.py:run`:
   - Line 74: `load_trace_candidates` globs and reads all traces.
   - Line 82: `evidence_io.load_evidence` globs and reads all traces and episodes.
   - Line 83: `load_trace_instances` globs and reads all traces a third time.
   - Line 95: `experiment_cmd._load` calls `_outcomes_by_occasion`, which globs and reads all traces and episodes a fourth time.
   A single invocation of `commontrace pilot` parses every trace file on disk 4 separate times.

3. **`memory/attention/query.py:102-146, 241-276` — Redundant Disk I/O and Dual-Pass in Retrieval**
   In `query.py:main`:
   - Line 403: `load_importances()` globs and reads every `lesson_*.md` file on disk and parses frontmatter.
   - Line 448: `check_staleness()` immediately globs and stats every `lesson_*.md` file again, and if mtimes changed, parses frontmatter again.
   Because `index.npz` stores only `slugs` and `embeddings` (and omits `importance` and `status`), `query.py` is forced to scan and parse all files on disk on every single query invocation.

4. **`commontrace/commands/_shellout.py:83-131` — High Subprocess and Process Startup Overhead**
   `commontrace query` and `commontrace index` shell out via `subprocess.run([sys.executable, script, ...])`. Every execution must bootstrap Python, import heavy dependencies (`sentence_transformers`, `torch`, `transformers`, `huggingface_hub`, `numpy`), and initialize models. This adds ~1.5 to 2.5 seconds of process startup latency per query.

5. **`commontrace/overlap.py:171-175, 296-298` — Unvectorized Pure-Python Loop in MinHash Jaccard Comparison**
   ```python
   175:     return sum(1 for x, y in zip(sig_a, sig_b) if x == y) / len(sig_a)
   ```
   In `build_report`, `estimate_jaccard` is called in a nested loop for every failure against every lesson: $O(N_{\text{failures}} \times N_{\text{lessons}})$. For 1,000 failures and 1,000 lessons with 128 permutations, this executes $10^6 \times 128 = 1.28 \times 10^8$ generator iterations in pure Python bytecode instead of using numpy vectorization or array equality.

6. **`commontrace/frontmatter.py:178-185` — Redundant Probe File Creation on Every New File Write**
   ```python
   178:     probe_path = os.path.join(target_dir, f".commontrace-umask-probe-{uuid.uuid4().hex}")
   179:     fd = os.open(probe_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
   ...
   184:         os.unlink(probe_path)
   ```
   To determine the current umask, `_new_file_mode` creates a probe file with a random UUID, stats it, closes it, and unlinks it on every new file write.

---

### 1.3 Security Vulnerabilities (R3)

1. **YAML Deserialization Audit**:
   Verified all `yaml.load` and `yaml.safe_load` calls:
   - `commontrace/frontmatter.py:152`: uses `yaml.load(fm_text, Loader=_StrictBoolLoader)` where `_StrictBoolLoader` subclasses `yaml.SafeLoader` and disables YAML entity expansion / anchors to prevent "Billion Laughs" algorithmic complexity attacks (lines 101-107).
   - `memory/attention/build_index.py:123`: uses `_StrictBoolLoader` (falling back to `yaml.safe_load`).
   - `memory/attention/query.py:99`: uses `_StrictBoolLoader` (falling back to `yaml.safe_load`).
   - `reference/measure_performance.py:120`: uses `yaml.safe_load` with regex fallback.
   No instances of unsafe `yaml.load(..., Loader=Loader)` or `yaml.unsafe_load` exist.

2. **Shell Injection Audit**:
   - `commontrace/commands/_shellout.py:125, 130`: uses `subprocess.run([sys.executable, script, *extra_args], env=env)`. Arguments are passed as an explicit array of strings; `shell=True` is NEVER used.
   - Zero occurrences of `os.system` or `os.popen` across `commontrace/` and `memory/`.

3. **Path Traversal Audit**:
   - `commontrace/lesson_io.py:51`: `SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")` prevents directory traversal (`../`) in lesson filenames.
   - `commontrace/validate.py:35-46`: `load_schema` checks `os.path.basename(name) == name`, explicitly forbids `\` and `:`, and verifies `path.startswith(base_dir + os.sep)`.
   - `commontrace/commands/install_cmd.py:218-237`: `_break_symlink` breaks symlinks before writing to prevent arbitrary file overwrites via pre-planted symlinks.
   - `commontrace/commands/capture_cmd.py:117`: `_id_suffix` sanitizes occasion IDs into `[A-Za-z0-9._-]` and strips `-.`.

4. **Numpy NPZ Deserialization Audit**:
   - `memory/attention/build_index.py:279`, `memory/attention/query.py:326`, and `commontrace/reference/measure_performance.py:846`: all load `.npz` using `np.load(..., allow_pickle=False)`.
   - `memory/attention/query.py:43, 339-347`: strictly validates `model_name == "multi-qa-mpnet-base-dot-v1"` against `_TRUSTED_MODEL_NAME` to prevent arbitrary model instantiation from modified `index.npz` files.

---

### 1.4 Dead and Unwired Code (R4)

1. **`commontrace/commands/query_cmd.py` — Unwired `--include-importance-floor` Flag**
   `memory/attention/query.py:297-301` implements `--include-importance-floor` (allowing users to set or disable the importance override floor). However, `commontrace query` does not expose this argument in its parser or forward it, preventing users from controlling or disabling the floor via the CLI.

2. **`commontrace/commands/query_cmd.py:189-196` — Unwired `--agent-type` Flag in Semantic Retrieval**
   `commontrace query` provides `--agent-type`, but when running semantic retrieval (the default mode), it prints:
   `[commontrace] --agent-type is not supported by the semantic retriever and was NOT applied. Use --lexical to filter by agent type.`
   Because `memory/attention/query.py` does not implement `--agent-type` filtering, the flag is unwired on the primary semantic retrieval path.

3. **`commontrace/commands/serve_cmd.py:21` — Inaccurate Help Text Pointing to Nonexistent Flag**
   `serve_cmd.py:21` states in its help text:
   `commontrace install --mcp writes the config entry that launches it.`
   In `install_cmd.py`, there is no `--mcp` flag; the correct flag is `--target generic-mcp` or `--target claude-code`.

4. **`commontrace/commands/init_cmd.py:24-69` — Missing `memory/attention/` Scaffolding**
   `commontrace init` scaffolds `memory/lessons`, `memory/traces`, and `memory/episodes`, but does not create `memory/attention` or initialize `index.npz`. When a user runs `query.py` or `commontrace query` immediately after `init`, it fails with:
   `[ERR] No index found at memory/attention/index.npz. Run build_index.py first.`

---

## 2. Logic Chain

1. **From Observation 1.1.1 to Bug Verification**:
   - In `build_index.py`, line 275 evaluates `os.path.getmtime(p)` inside a list generator over `glob.glob`.
   - In concurrent multi-agent systems, lessons are approved, archived, or deleted concurrently.
   - If a file is unlinked between `glob.glob` and `os.path.getmtime`, Python raises `FileNotFoundError`.
   - Therefore, `build_index.py` can crash intermittently during index builds.

2. **From Observation 1.1.3 to Runtime Error**:
   - `compute_freshness` computes `cutoff = now - timedelta(days=90)` where `now` defaults to `datetime.datetime.now()` (naive).
   - `_parse_last_hit` produces naive datetimes.
   - If a caller supplies a timezone-aware UTC datetime for `now`, `cutoff` is aware.
   - Python raises `TypeError` when comparing naive `hit` with aware `cutoff`.

3. **From Observation 1.1.6 to Semantic Misclassification**:
   - `load_active_lessons` loads all lessons without checking `fm.get("status") == "active"`.
   - Callers relying on this function expecting active lessons either incur redundant defensive filters (as in `taxonomy.py:71` and `reliability.py:291`) or incorrectly include archived/review lessons in active counts.

4. **From Observation 1.2.1 to Performance Bottleneck**:
   - `_find_trace_by_occasion` scans and opens every `.md` file in `memory/traces/` on every capture with `--occasion-id`.
   - File opens and YAML parsing in Python take ~0.5ms to 1ms per file.
   - For 2,000 traces, each capture call takes 1 to 2 seconds of pure sequential disk I/O just to find if the occasion exists.
   - Since filenames are generated with `_id_suffix(trace_id)`, targeted globbing or hashing reduces this to $O(1)$.

5. **From Observation 1.2.2 to Performance Bottleneck**:
   - `commontrace pilot` calls 4 loaders sequentially, each globbing and reading the trace directory independently.
   - Unifying or caching trace loading across `pilot_cmd.py` reduces disk read operations by 75%.

---

## 3. Caveats

- Investigation is strictly read-only; no code modifications were applied during this survey phase.
- Hub database internals and SQL queries are outside Explorer 1 scope (covered by Explorer 2 and 3).
- Code-review reference pipeline execution was surveyed via scripts (`SKILL.md`, `measure_performance.py`, `build_index.py`, `query.py`), not by driving live LLM sessions.

---

## 4. Conclusion

The core CLI (`commontrace/`) and attention memory subsystem (`memory/`) are structurally solid, clean, and conform to the CommonTrace protocol specifications. Security posture is high (no `shell=True`, safe YAML loading, path traversal defenses, model whitelist).

However, actionable defects and optimization opportunities exist across four distinct areas:
1. **Bugs & Edge Cases**: 8 distinct defects involving unhandled I/O exceptions in `build_index.py` and `pilot_metrics.py`, datetime naive/aware comparison crashes, regex boundary leaks in `trace_io.py`, ASCII-only lexical tokenization in `measure_performance.py`, and status filtering omissions in `evidence_io.py`.
2. **Performance**: Significant I/O bottlenecks in `capture_cmd.py` (full directory walk per capture), `pilot_cmd.py` (quad-walk of traces), `query.py` (dual-pass read of all lessons on every query), subprocess invocation overhead in `_shellout.py`, and unvectorized Jaccard loops in `overlap.py`.
3. **Security**: Maintained strong guards, but umask probe file creation in `frontmatter.py` introduces unnecessary disk churn.
4. **Dead / Unwired Code**: `--include-importance-floor` missing from `query_cmd.py`, `--agent-type` ignored in semantic queries, outdated help text in `serve_cmd.py`, and missing initial `memory/attention` setup in `init_cmd.py`.

---

## 5. Verification Method

To independently verify all observations and conclusions:

1. **Verify Existing Tests**:
   ```bash
   python -m pytest tests/ -v
   ```
   Result: 1079 passed, 30 skipped.

2. **Verify Datetime Naive/Aware Crash in `compute_freshness`**:
   ```bash
   python -c "
   import datetime
   from commontrace.reference.measure_performance import compute_freshness
   lessons = {'test': {'last_hit': '2026-07-01'}}
   now_utc = datetime.datetime.now(datetime.timezone.utc)
   compute_freshness(lessons, now=now_utc)
   "
   ```
   Result: Raises `TypeError: can't compare offset-naive and offset-aware datetimes`.

3. **Verify Trace I/O Section Lookahead Leak**:
   ```bash
   python -c "
   from commontrace.trace_io import _first_wins
   body = '## Context\nProblem context\n\n## Notes\nExtra notes\n\n## Solution\nSolution text\n'
   sections = _first_wins(body)
   print(repr(sections['context']))
   "
   ```
   Result: Prints `'Problem context\n\n## Notes\nExtra notes'`, confirming `## Notes` is leaked into `context`.

4. **Verify `load_active_lessons` Loads Inactive Lessons**:
   ```bash
   python -c "
   from commontrace import evidence_io, paths
   lessons = evidence_io.load_active_lessons(paths.resolve_root())
   statuses = {l.get('status') for l in lessons}
   print('Statuses loaded:', statuses)
   "
   ```

5. **Verify Full-Trace Scan in `capture_cmd.py`**:
   Inspect `commontrace/commands/capture_cmd.py:126-140` (`_find_trace_by_occasion`).

6. **Verify System Diagnostics**:
   ```bash
   commontrace doctor
   commontrace bench
   ```

---

## Proposed Remediation Table

| Finding ID | File & Line Number | Severity | Proposed Remediation |
|---|---|---|---|
| **R1-1** | `memory/attention/build_index.py:274` | Medium | Wrap `os.path.getmtime(p)` in helper with `try...except OSError: return 0.0`. |
| **R1-2** | `commontrace/reference/pilot_metrics.py:56` | Medium | Wrap `open()` and `read()` in `load_traces` with `try...except OSError: continue`. |
| **R1-3** | `commontrace/reference/measure_performance.py:984` | Medium | Ensure `now` and `hit` share timezone awareness before comparison. |
| **R1-4** | `commontrace/reference/measure_performance.py:919` | Low | Replace ASCII `_TOKEN_RE` with `\w+` (re.UNICODE), matching `_lexical.py`. |
| **R1-5** | `commontrace/trace_io.py:20` | Medium | Change regex lookahead to `(?=\n##\s|\Z)` to prevent swallowing non-standard sections. |
| **R1-6** | `commontrace/evidence_io.py:27` | Medium | Filter `if fm.get("status") == "active"` in `load_active_lessons`, or add a `status` parameter. |
| **R1-7** | `commontrace/commands/taxonomy_cmd.py:21` | Low | Use `_similarity_threshold` validator to ensure `0 < threshold <= 1`. |
| **R1-8** | `commontrace/cli.py:120` | Low | Expand exception tuple to catch `TypeError` and `IndexError` gracefully. |
| **R2-1** | `commontrace/commands/capture_cmd.py:126` | High | Probe `*_{_id_suffix(occasion_id)}.md` before falling back to full directory scan. |
| **R2-2** | `commontrace/commands/pilot_cmd.py:74` | Medium | Cache loaded traces in `run()` to avoid reading the directory 4 times. |
| **R2-3** | `memory/attention/query.py:102, 241` | Medium | Collect file mtimes during `load_importances` pass to eliminate second scan in `check_staleness`. |
| **R2-4** | `commontrace/commands/_shellout.py:83` | Medium | Allow in-process execution when dependencies are already loaded to save ~2s per query. |
| **R2-5** | `commontrace/overlap.py:175` | Medium | Vectorize MinHash signature comparison using numpy array equality when available. |
| **R2-6** | `commontrace/frontmatter.py:178` | Low | Avoid probe file creation on every write; cache or query default directory permissions. |
| **R4-1** | `commontrace/commands/query_cmd.py:27` | Low | Add `--include-importance-floor` argument to `query_cmd.py` and forward to `query.py`. |
| **R4-2** | `commontrace/commands/query_cmd.py:189` | Low | Implement `--agent-type` filtering in `memory/attention/query.py` or document parity. |
| **R4-3** | `commontrace/commands/serve_cmd.py:21` | Low | Fix help text description from `commontrace install --mcp` to `commontrace install --target generic-mcp`. |
| **R4-4** | `commontrace/commands/init_cmd.py:33` | Low | Create `memory/attention/` directory and call `build_index.py` (or write empty index) during `init`. |
