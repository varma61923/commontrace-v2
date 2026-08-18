# Agent Guidance — commontrace

This file is for AI coding agents picking up this codebase. It is platform-agnostic
and works with any agent framework (Devin, Claude Code, Cursor, OpenHands, custom).

---

## What this project is

Two layers:

1. **The CommonTrace Protocol** (`protocol/PROTOCOL.md`) — an agent-agnostic spec
   (Capture → Structure → Extract → Validate → Store → Inject → Measure) and two
   JSON Schemas (`protocol/schemas/trace.schema.json`,
   `protocol/schemas/lesson.schema.json`). Implemented as a pip-installable CLI in
   `commontrace/` (`commontrace init/install/capture/trace/lesson/query/index/bench/sync/doctor`).
2. **The code-review reference profile** (`SKILL.md`) — a double-review agent
   pipeline where:

1. An **Implementer (Agent A)** writes the code and commits.
2. An independent **Reviewer (Agent B)** audits against success criteria.
3. They iterate (max 3 rounds) until CONFORM or the orchestrator arbitrates.
4. **Alpha** injects relevant past lessons before each run (Phase 0).
5. **Omega + Lambda** extract and validate new lessons after each run (Phases 10-11).

The full pipeline spec lives in `SKILL.md`. Architecture diagrams are in `assets/`.

---

## Key files

| File | Purpose |
|---|---|
| `protocol/PROTOCOL.md` | Canonical, implementation-independent protocol spec — read this first if you're not doing coding-agent double-review |
| `protocol/schemas/*.json` | JSON Schema for `Trace` and `Lesson` |
| `commontrace/cli.py` | CLI entry point (`commontrace <subcommand>`) |
| `SKILL.md` | Code-review reference profile pipeline spec |
| `README.md` | User-facing quick-start and reference |
| `DOCUMENTATION.md` | Deep design doc on the code-review profile, research refs, roadmap |
| `requirements.txt` | Python deps for the code-review profile's attention layer (numpy, sentence-transformers, PyYAML) |
| `install.sh` | Setup script for the code-review profile (SKILL.md route) |
| `memory/INDEX.md` | Index of all stored lessons/traces/episodes |
| `memory/attention/build_index.py` | Rebuild semantic embedding index |
| `memory/attention/query.py` | Query lessons by cosine similarity |
| `benchmark/measure_performance.py` | Memory health benchmark |

---

## Build / verification commands

```bash
# Install the commontrace CLI (protocol client)
python3 -m pip install -e .
commontrace doctor

# Install dependencies for the code-review profile's attention layer
python3 -m pip install -r requirements.txt

# Run tests (pytest — covers benchmark, frontmatter contract, and the CLI)
python3 -m pytest tests/ -v

# Rebuild attention index after editing lessons
python3 memory/attention/build_index.py

# Query the index (smoke test)
python3 memory/attention/query.py "test query" --top-k=5

# Run benchmark
python3 benchmark/measure_performance.py

# Save benchmark snapshot (JSON + markdown)
python3 benchmark/measure_performance.py --save

# HTML benchmark report
python3 benchmark/measure_performance.py --html
```

The test suite (`tests/`) is the primary code quality gate. The benchmark
(`measure_performance.py`) is the quality signal for the memory system itself.

---

## Path configuration

All Python scripts auto-detect their root from their own file location.
No environment variable needed when running from the repo checkout.

To override: `export COMMONTRACE_ROOT=/path/to/commontrace`

**Do not** hardcode any platform-specific paths (e.g., `~/.claude/`, `~/.cursor/`)
in any file. Use `COMMONTRACE_ROOT` or relative paths from the script location.

---

## Memory conventions

- Lessons live in `memory/lessons/lesson_<slug>.md` — YAML frontmatter + markdown body.
- Episodes live in `memory/episodes/YYYY-MM-DD_<slug>.md` — one per `/commontrace` run.
- `memory/INDEX.md` is the hierarchical index — update it when adding/removing lessons.
- `memory/attention/index.npz` is **generated** (gitignored) — rebuild with `build_index.py`.
- Template files (`lesson_template.md`, `episode_template.md`) are excluded from indexing.

---

## What NOT to do

- Do **not** commit `memory/attention/index.npz` (it is gitignored).
- Do **not** hardcode platform-specific paths in any file.
- Do **not** modify `memory/` during a live `/commontrace` run — Alpha reads it at Phase 0,
  Omega writes it at Phase 10; concurrent edits cause data loss.
- Do **not** use `git stash`, `git clean`, or `git restore` inside sub-agent briefs —
  this can erase uncommitted implementation work (known incident, cf. SKILL.md Phase 4).

---

## Coding style

- Python 3.10+, stdlib-first.
- `argparse` for CLI flags, `os.path.join` for paths (no f-strings for paths).
- YAML frontmatter in lessons/episodes: use `PyYAML` (`yaml.safe_load`); the benchmark
  has a regex fallback for environments without PyYAML.
- No external dependencies beyond `requirements.txt`.
- All scripts use auto-detection for the project root — no hardcoded paths.

---

## Platform-specific notes

### Devin
- Set `COMMONTRACE_ROOT` only if running scripts from outside the repo checkout.
- The `.devin/skills/commontrace/SKILL.md` file registers this as a Devin skill.

### Claude Code
- Install to `~/.claude/skills/commontrace` with `./install.sh --dest ~/.claude/skills/commontrace`.
- Invoke with `/commontrace <task>`.

### Other platforms
- Point the orchestrator agent at `SKILL.md` as the operational spec.
- Use the "Platform Mapping" section in `SKILL.md` to adapt API calls.
- The `memory/` directory is the durable state between sessions; treat it as a database.
