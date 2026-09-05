# Comprehensive Technical Survey Report: Benchmark, Test Suite, Repo-wide Security, and Dead Code

**Agent**: Survey Explorer 3 (`teamwork_preview_explorer_survey_3`)  
**Parent**: Orchestrator (`bf1be23f-c3e9-4aff-850c-99fca8f4e1d9`)  
**Date**: 2026-09-05T13:25:00Z  
**Scope**: Deep technical survey of `benchmark/` (`measure_performance.py`, `commontrace bench`), `tests/` (existing coverage, missing regression test areas), repository-wide security scan, and repository-wide dead code analysis.

---

## 1. Observation

### 1.1 Benchmark Integrity & Performance (`commontrace bench`, `benchmark/`, `commontrace/reference/`)

1. **Benchmark Location & CLI Integration**:
   - `benchmark/README.md:3-9`: The benchmark scripts moved into the installed package at `commontrace/reference/measure_performance.py` and `commontrace/reference/pilot_metrics.py` so that `commontrace bench` works for wheel installs without a repository checkout.
   - `commontrace/commands/_shellout.py:125-130`: Invocations shell out via `subprocess.run([sys.executable, script, *extra_args], env=env)`. Notice that `env["PYTHONUTF8"] = "1"` is only set on line 124 when `capture=True`, leaving `capture=False` (the default for `bench` streaming to stdout) vulnerable to Windows console/redirection default codepage decoding errors.

2. **Benchmark Report Collision Sorting Flaw**:
   - `commontrace/reference/measure_performance.py:1059-1067`:
     ```python
     base = ts.strftime("%Y-%m-%d_%H%M%S_%f")
     path = os.path.join(out_dir, f"{base}.json")
     suffix = 1
     while os.path.exists(path):
         path = os.path.join(out_dir, f"{base}-{suffix}.json")
         suffix += 1
     ```
   - `commontrace/reference/measure_performance.py:1081`:
     ```python
     for p in sorted(glob.glob(os.path.join(d, "*.json"))):
     ```
   - In ASCII, `'-'` is ASCII 45 and `'.'` is ASCII 46. Because `'-' < '.'`, `"YYYY-MM-DD_HHMMSS_ffffff-1.json"` sorts *before* `"YYYY-MM-DD_HHMMSS_ffffff.json"`. Any collision-resolved report is therefore sorted as *older* than the original run in `load_stored_reports()`, inverting chronological order in `run_diff()` and `run_history()`.

3. **TOCTOU Race Condition on Report Persistence**:
   - `commontrace/reference/measure_performance.py:1065-1068`: The loop tests `os.path.exists(path)` and then executes `with open(path, "w", encoding="utf-8") as fh: json.dump(clean_report, fh)`. Under concurrent runs (such as multiple agents or parallel CI workers), two processes can see that the path is non-existent simultaneously, causing race conditions and partial file overwrite rather than atomic creation (e.g. `tempfile.NamedTemporaryFile` + `os.replace` or `os.O_CREAT | os.O_EXCL`).
   - `commontrace/reference/measure_performance.py:1739-1741`: HTML reports use second-level granularity (`f"{now.strftime('%Y-%m-%d_%H%M%S')}.html"`) without any collision loop or atomic rename; two runs within the same second silently overwrite HTML reports.

4. **Timezone Inconsistency and Naive Datetime Skew**:
   - `commontrace/reference/measure_performance.py:1666`: `now = datetime.datetime.now()` produces a timezone-naive local timestamp.
   - `commontrace/reference/measure_performance.py:978`:
     ```python
     return datetime.datetime.strptime(text[:len("2026-01-01T00:00:00")], fmt)
     ```
     `_parse_last_hit` truncates strings at 19 characters, discarding any trailing `'Z'` or `'+00:00'` timezone offsets, producing a naive timestamp.
   - `commontrace/reference/measure_performance.py:994-996`:
     ```python
     now = now or datetime.datetime.now()
     cutoff = now - datetime.timedelta(days=FRESHNESS_WINDOW_DAYS)
     ```
     Comparing naive local `now` with naive UTC-derived timestamps creates an offset equal to the local timezone differential. In contrast, `memory/attention/query.py:495` explicitly records `"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")` and warns that naive timestamps cannot be sorted or compared across runners in different timezones.

5. **JSON Mode Output Contract Violation on Empty Corpus**:
   - `commontrace/reference/measure_performance.py:1662-1664`:
     ```python
     if not episodes:
         print("Not enough episodes to compute. Run /commontrace a few times first.")
         sys.exit(0)
     ```
   - `commontrace/reference/pilot_metrics.py:242-244`:
     ```python
     if not traces:
         print("No traces with outcome data found. Run `commontrace capture` with outcome flags first.")
         sys.exit(0)
     ```
     When invoked with `--json`, both scripts print plain English sentences to stdout and exit 0. Any automated caller attempting `json.loads(proc.stdout)` will raise `json.JSONDecodeError`.

6. **Throughput and File I/O Redundancy in `compute_transfer_gap`**:
   - `commontrace/reference/measure_performance.py:537-551`:
     ```python
     def resolve_project(slug):
         if slug in episode_project:
             return episode_project[slug]
         clean_slug = slug[:-3] if slug.endswith(".md") else slug
         path = os.path.join(BASE_DIR, "episodes", f"{clean_slug}.md")
         if os.path.exists(path):
             with open(path, encoding="utf-8-sig") as fh:
                 fm = parse_frontmatter(fh.read())
             return (fm or {}).get("project")
         return None
     ```
     `resolve_project` is called inside a nested loop over all hits of all episodes. While `load_episodes(n)` has already read and parsed episodes, if a hit points to an episode outside the loaded window, `resolve_project` repeatedly performs disk checks, file opening, and frontmatter parsing on every hit with zero caching or memoization.
   - In addition, episode files on disk are prefixed with dates (e.g. `2026-07-01_example-api-pagination.md`). If `source_traces` only records the slug without the date, `os.path.join(BASE_DIR, "episodes", f"{clean_slug}.md")` fails to find the file and falsely reports the hit as untraceable.

7. **HTML Report Parser Quote Escaping Defect**:
   - `commontrace/reference/measure_performance.py:1404`:
     ```python
     text = html.escape(text, quote=False)
     ```
     Because `quote=False` is set, double and single quotes are not escaped. If markdown elements contain unescaped quotes or if inline code regex substitutions (`re.sub(r"`([^`]+)`", r"<code>\1</code>", text)`) contain asterisks, subsequent bold/italic regex matches can span across tag boundaries and generate malformed HTML tags.

8. **Default `--strict` Alert Behavior on New Installations**:
   - Running `commontrace bench --no-save --strict` on a clean repository checkout exits with code `2`.
   - Verbatim output:
     `- **WARNING**: 1/2 lessons never hit (50.0% > threshold 30.0%) — consider archiving stale lessons`
   - Default `--threshold-never-hit=0.3` fails because the repository ships with 2 example lessons, 1 of which has never been hit (50% > 30%). Also, `compute_alerts()` unimodal importance distribution alert triggers unconditionally on any store with only 1 lesson (`max_count / total_imp == 1.0 >= 0.95`).

---

### 1.2 Test Suite Audit (`tests/`, `pytest`)

1. **Baseline Execution**:
   - Command: `pytest tests/ -v`
   - Result: `1076 passed, 33 skipped in 122.53s (0:02:02)`.
   - Exit code: `0`.
   - Skips breakdown:
     - 32 tests in `tests/test_attention_query.py`: `could not import 'numpy': No module named 'numpy'`.
     - 1 test in `tests/test_mcp_server.py`: ``commontrace serve` needs the MCP SDK: pip install 'commontrace[serve]'`.

2. **Untested CLI Command**:
   - `commontrace/commands/index_cmd.py`: Contains 34 lines implementing `commontrace index` (`--force`, `--dest`).
   - Cross-referencing `index_cmd` across the entire `tests/` directory revealed exactly 0 references. No test exists for rebuilding the index via the CLI, validating error messages when attention dependencies are absent, or passing `--force` and `--dest`.

3. **Untested Target Variants in `install_cmd.py`**:
   - `commontrace/commands/install_cmd.py:273-337` defines installation logic for 6 targets: `claude-code`, `cursor`, `devin`, `windsurf`, `generic-mcp`, and `generic`.
   - `tests/test_mcp_server.py` and `tests/test_cli.py` only test the `claude-code` target for MCP JSON generation. Targets `cursor` (writing `.cursor/rules/commontrace.mdc` and hub example), `windsurf` (writing `.windsurf/rules/commontrace.md`), `devin` (writing `.devin/skills/commontrace/SKILL.md`), and `generic` are completely untested.

4. **Untested Internal Modules & Error Paths**:
   - `commontrace/report_html.py`: Shared HTML wrapper (`wrap_page`, `stat_card`) used by `impact.py`, `pilot.py`, and `taxonomy.py` has no dedicated test file.
   - `commontrace/evidence_io.py`: Core I/O module loading lessons, episodes, and traces has no dedicated unit tests for corrupted files, empty directories, or missing frontmatter fields.
   - `commons/eval/` (`representations.py`, `retrieval_tiers.py`, `run.py`, `search_modes.py`): Zero tests in `tests/`.
   - `hub/bench_retrieval.py`: Zero tests in `hub/tests/` or `tests/`.

---

### 1.3 Repository-wide Security Scan

1. **YAML Loading & Deserialization**:
   - Searched pattern: `yaml.load`, `pickle`, `eval`, `exec`.
   - In `commontrace/frontmatter.py:152`, `memory/attention/build_index.py:123`, and `memory/attention/query.py:99`: `yaml.load(fm_text, Loader=_StrictBoolLoader)` is used with `# nosec B506`. `_StrictBoolLoader` subclasses `yaml.SafeLoader` and only overrides implicit conversions for booleans and timestamps. This is safe against arbitrary code execution.
   - In `commontrace/reference/measure_performance.py:120`: `yaml.safe_load(fm_text)` is used. While safe against RCE, it is vulnerable to YAML 1.1 type confusion:
     - Keys like `domain: NO` or `domain: on` parse as booleans `False` and `True`.
     - Fields like `commit_sha: 0000000` parse as integer `0` (as observed in `bench --json`).

2. **Shell Injection Vectors**:
   - Searched patterns: `shell=True`, `os.system`, `os.popen`.
   - Result: 0 instances found in the entire repository.
   - All subprocess calls use explicit vector arguments: `subprocess.run([sys.executable, script, *extra_args], env=env)`.

3. **Path Traversal Vulnerabilities**:
   - `capture_cmd.py:117-123`: Uses `_id_suffix` (`re.sub(r"[^A-Za-z0-9._-]+", "-", trace_id)`) and `_slugify` (`re.sub(r"[^a-z0-9]+", "-", title.lower())`) to strictly sanitize file paths.
   - `lesson_cmd.py:101-107`: Validates slugs against `_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")`.
   - `revision.py:112`: Enforces `slug.replace('\\', '/')` and rejects any slug containing `'/'` or `'..'`.
   - **Vulnerability Found in `install_cmd.py:268-280`**:
     ```python
     root = paths.resolve_root()
     dest = os.path.abspath(args.dest)
     ...
     _write_local_mcp(dest, root)
     ```
     `root` is resolved with `paths.resolve_root()` without passing `args.dest`. When `--dest` points to a separate directory, `root` defaults to `os.getcwd()` or `$COMMONTRACE_ROOT`, which hardcodes the caller's repository path into the generated `.mcp.json` at `dest`, potentially leaking internal directory structures or pointing external agents to incorrect paths.

4. **File Permissions**:
   - `commontrace/frontmatter.py:179`: Correctly creates umask probe files with `0o666` and lock files with `0o644`.
   - In `commontrace/reference/measure_performance.py:1068, 1740`, `pilot_metrics.py:269`, `impact_cmd.py:73`, `pilot_cmd.py:184`, and `taxonomy_cmd.py:59`: Files are written with default `open(path, "w", encoding="utf-8")`. While standard, sensitive reports in multi-tenant environments are not created with explicit restrictive permissions (`0o600`).

---

### 1.4 Repository-wide Dead Code Analysis

1. **Unreferenced Standalone Scripts**:
   - `commons/eval/representations.py` (259 lines): Prototype research script evaluating alternative token representations for Commons. Never imported by any module, never called by the CLI, not packaged in `pyproject.toml`.
   - `commons/eval/retrieval_tiers.py` (204 lines): Standalone evaluation script comparing local vs hub retrieval tiers. Never imported or referenced in production code.
   - `commons/eval/search_modes.py` (189 lines): Standalone evaluation script for search modes. Unreferenced.
   - `hub/bench_retrieval.py` (385 lines): Benchmarks Hub search against probes. Never imported or tested in `hub/tests/`.

2. **Deprecated No-op Arguments**:
   - `commontrace/commands/bench_cmd.py:20-22` & `commontrace/reference/measure_performance.py:1580-1583`:
     `--save` is maintained as a deprecated no-op because benchmark JSON reports are now saved by default.

---

## 2. Logic Chain

```
[Observation 1.1.2: persist_report appends '-1', '-2' to base timestamp]
  └──> [Fact: In ASCII table, '-' (45) < '.' (46)]
        └──> [Observation 1.1.2: load_stored_reports sorts filenames with sorted(glob.glob())]
              └──> [Deduction: 'base-1.json' sorts BEFORE 'base.json', corrupting chronology in --diff and --history]

[Observation 1.1.3: persist_report checks os.path.exists() then open(w)]
  └──> [Fact: Check-then-act without atomic locks or O_EXCL is non-atomic]
        └──> [Deduction: Parallel CI workers or multiple agents can clobber and corrupt benchmark JSON history]

[Observation 1.1.4: now = datetime.datetime.now() vs UTC dates in frontmatter/telemetry]
  └──> [Fact: Naive datetimes assume local system timezone]
        └──> [Deduction: Freshness calculations in compute_freshness skew by the runner's timezone offset]

[Observation 1.1.5: Empty episodes/traces in bench/pilot print strings and exit 0]
  └──> [Fact: Consumers passing --json expect valid JSON strings on stdout]
        └──> [Deduction: Automated pipelines calling `json.loads(stdout)` fail with JSONDecodeError]

[Observation 1.1.6: resolve_project in compute_transfer_gap lacks caching]
  └──> [Fact: Episodes and traces can have multiple cross-references across hundreds of runs]
        └──> [Deduction: Redundant file opens and frontmatter parsing create quadratic I/O overhead]

[Observation 1.2.2: index_cmd.py has 0 references in tests/]
  └──> [Fact: index_cmd shells out to memory/attention/build_index.py]
        └──> [Deduction: Regressions in CLI index rebuilding or dependency handling cannot be caught by CI]

[Observation 1.3.3: install_cmd.py:268 calls paths.resolve_root() without args.dest]
  └──> [Fact: paths.resolve_root(args.dest) correctly inspects the destination directory]
        └──> [Deduction: Running `commontrace install --dest /path` configures local MCP servers with the cwd rather than the target store root]

[Observation 1.4.1: commons/eval/* and hub/bench_retrieval.py have no importers or tests]
  └──> [Fact: pyproject.toml excludes commons and hub from package build]
        └──> [Deduction: These scripts are orphaned development artifacts or need clear documentation as manual research harnesses]
```

---

## 3. Caveats

1. **Optional Dependencies in Test Environment**:
   - `numpy` and `sentence-transformers` were not installed in the survey environment, skipping 32 tests in `test_attention_query.py`.
   - `mcp` SDK was not installed, skipping 1 test in `test_mcp_server.py`.
   - `sqlalchemy` and `asyncpg` were not installed in the main test environment, which prevented running `hub/tests/` without setting up a Postgres container.
2. **Backward Compatibility Guarantee**:
   - In Requirement 4 (Dead Code), documented public CLI flags like `--save` are retained by design to avoid breaking user scripts, even though they are runtime no-ops.
   - `commons/eval/` scripts are valuable research artifacts (cited in `DOCUMENTATION.md` and `STRATEGY.md`) and should not be deleted outright without orchestrator consultation.

---

## 4. Conclusion

The `commontrace-v2` codebase demonstrates strong architectural discipline, rigorous schema validation, and thorough defensive programming across core workflows (1076 tests passing). However, this survey identified critical defects and risks across all 4 audit dimensions:

1. **Benchmark Integrity (High Priority)**:
   - File collision sorting bug in `persist_report` reversing diff/history report order.
   - TOCTOU non-atomic write in `persist_report` and HTML reports.
   - Non-JSON text emitted under `--json` when data is missing.
   - Redundant disk I/O in `compute_transfer_gap`.
   - False positive `--strict` failure on clean checkouts due to never-hit thresholds.
2. **Test Suite Coverage (Medium Priority)**:
   - Complete absence of test coverage for `commontrace/commands/index_cmd.py`.
   - Missing coverage for `install_cmd.py` targets (`cursor`, `devin`, `windsurf`, `generic`).
   - Missing unit tests for `commontrace/report_html.py` and `evidence_io.py`.
3. **Security Hardening (Medium Priority)**:
   - Root path resolution bug in `install_cmd.py` embedding host cwd instead of destination.
   - Unescaped quotes in `measure_performance.py` HTML rendering (`quote=False`).
   - Inconsistent YAML 1.1 scalar loading in `measure_performance.py` vs `frontmatter.py`.
4. **Dead / Unwired Code (Low Priority)**:
   - Orphaned evaluation scripts in `commons/eval/` and `hub/bench_retrieval.py`.

---

## 5. Verification Method

### 5.1 Independent Verification Commands

1. **Run Full Test Suite Baseline**:
   ```bash
   pytest tests/ -v
   ```
   *Expected*: 1076 passed, 33 skipped.

2. **Verify Benchmark Collision Sorting Flaw**:
   Run python in repo root:
   ```python
   import os
   base = "2026-09-05_120000_123456"
   f1 = f"{base}.json"
   f2 = f"{base}-1.json"
   print("f2 < f1:", f2 < f1) # Returns True!
   ```

3. **Verify JSON Output Violation on Empty Input**:
   ```bash
   # In an empty directory or without episodes:
   commontrace bench --json --no-save
   commontrace bench --pilot --json
   ```
   *Expected failure*: Emits plain text instead of JSON string `{"error": ...}`.

4. **Verify `install_cmd.py` Root Resolution Bug**:
   Inspect `commontrace/commands/install_cmd.py` line 268:
   ```python
   root = paths.resolve_root() # Missing args.dest!
   ```

5. **Verify Index Command Test Gap**:
   ```bash
   grep -rn "index_cmd" tests/
   ```
   *Expected*: Returns 0 results.

