# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`commontrace import`** (`commontrace/import_data.py` +
  `commands/import_cmd.py`): bulk-import an existing JSONL or CSV export
  into `memory/traces/`, per the pilot deck's "What we connect to" /
  "start from your historical traces, no infrastructure replacement"
  pitch. Field-name mapping is configurable (a real export's column names
  are whatever the source system calls them); malformed or
  missing-required-field rows are skipped and reported per-row rather than
  failing the whole batch; `resolved`/`escalated`/`repeated_error`/
  `frustration_signal`/`tokens_used`/`llm_calls` columns populate
  `Trace.outcome` automatically if present. `--dry-run` previews without
  writing. Deliberately a generic format-level importer, not a set of
  vendor-specific connectors (Zendesk, Salesforce, Datadog, ...) this
  codebase has no way to test against a real vendor API for.
- **A generic Curator/Validator loop for any `agent_type`**
  (`commontrace distill`, `commontrace lesson approve|reject`). Previously
  "Extract lessons" / "Validate" (protocol/PROTOCOL.md §2, §6) only had a
  concrete implementation for the code-review profile's Omega/Lambda
  subagents, which only run inside a live Claude Code session. `distill`
  clusters `memory/traces/*.md` by word-overlap similarity (pure Python, no
  LLM call, no API key) and writes candidate lessons at `status: review`
  only — never `active`; re-running it skips traces already referenced by
  an existing lesson's `source_traces`. `lesson approve`/`lesson reject`
  are the only way a `review` lesson becomes `active`/`archived`, and both
  refuse to act on a lesson not already in `review`.
- **A dependency-free retrieval fallback** (`commontrace/retrieval.py`).
  `commontrace query` previously *required* the optional `[attention]`
  extra and failed outright without it — "Local tier remains file-based for
  agents that can only read/write files" (PROTOCOL.md §8) wasn't actually
  true for the Retriever role. It now falls back automatically to a
  pure-Python lexical (word-overlap) ranker, or `--lexical` forces it
  explicitly.
- protocol/PROTOCOL.md §6's Roles table gained a "Generic CLI reference"
  column pointing Curator/Validator/Retriever at the commands above.
- **The CommonTrace Hub server (`hub/`).** Previously `protocol/PROTOCOL.md`
  described a Hub as already in production while `commontrace sync` made no
  network call at all — this closes that gap with a real implementation: an
  MCP server (`search_traces`, `contribute_trace`, `get_trace`, `vote_trace`,
  `amend_trace`, `list_tags`) over streamable-HTTP, backed by Postgres
  (SQLAlchemy 2.0 + Alembic), with every read/write scoped to the calling
  org's `org_id` at the query layer, API-key-per-org auth (argon2-hashed,
  rotatable), and abuse controls (schema/size validation, per-org rate
  limiting, a quarantine state for suspect contributions). See
  `hub/README.md` for setup and the design decisions worth knowing about
  before extending it, especially "Tenant isolation vs. the cross-org
  commons pitch."
- `commontrace/hub_client.py` + `commontrace sync --push`/`--pull`: the
  client half of the bridge, now a real implementation instead of printed
  instructions — pushes local `active` lessons to the Hub via
  `contribute_trace` (recording `hub_trace_id` back into the lesson
  frontmatter) and pulls `search_traces` results into `memory/traces/` as
  candidates for `commontrace lesson new`. New `commontrace[hub-sync]`
  optional extra for the client dependency.
- `DATA_RETENTION.md` now documents the Hub tier's actual tables and
  `org_id` scoping instead of stating no verified system existed to
  describe; still explicitly flags org-level data deletion as unimplemented
  and the cross-org "commons" deletion question as an open business decision.
- Benchmark credibility (`benchmark/measure_performance.py`,
  `memory/attention/query.py`; see `benchmark/STATUS.md` §5 P2–P5, P8):
  every run now persists to `memory/benchmark_reports/*.json`
  (`schema_version`-tagged) with new `--diff`/`--history`/`--strict` modes;
  configurable alert thresholds surface as a report-level "Alerts" section;
  `memory/alpha_telemetry.jsonl` + a new "Operational Cost" report section
  instrument retrieval latency/token cost; a new "Semantic near-duplicates"
  section flags cosine->0.85 lesson pairs as merge candidates
  (recommendation-only); and `SKILL.md`'s episode guidance now tags
  sub-projects distinctly so `transfer_gap` can become non-zero going
  forward (no existing episode file was retagged retroactively). No
  existing metric definition, formula, or exclusion rule changed.
- CI (`.github/workflows/ci.yml`) running the test suite and a `ruff` lint pass
  on Python 3.10, 3.11, and 3.12, both for the core install (`pip install -e .`)
  and the `dev` extra (`pip install -e ".[dev]"`).
- `LICENSE` file (MIT) at the repo root, matching the license already declared
  in `pyproject.toml`.
- A support matrix in `README.md` for `commontrace install --target <...>`
  documenting, per target, what file(s) are written and how their format was
  verified (template-vs-published-spec, not live-tested against the running
  platform).
- `[INFO]` severity in `commontrace doctor`, for conditions that are expected
  and not actionable in a normal client install (e.g. the optional `attention`
  extra not being installed, or reference scripts only present in a source
  checkout) — previously these were indistinguishable from real `[WARN]`s.

### Changed
- `ruff` added to the `dev` optional-dependency group, with an explicit
  `[tool.ruff.lint] select = ["E", "F", "W", "I"]` policy rather than
  whatever a given `ruff` release's default rule set happens to include —
  needed because this repo's `ruff` version's real defaults pull in far
  more than pyflakes/pycodestyle and were failing CI outright.
- README install-target quick-reference and file-layout table point at the new
  support matrix instead of asserting untested platform behavior.
- Documentation no longer implies `pip install commontrace` (bare, from PyPI)
  works today; `pip install -e .` from a repo checkout is the only currently
  verified install path, and PyPI publication is called out as a future step
  (see `protocol/PROTOCOL.md` §8 and `README.md`).
- `README.md` no longer describes the Hub as "production" infrastructure
  external to this repo; it now points at `hub/` as the (self-hosted, not
  hosted-by-this-project) server implementation.
- The `[attention]` optional extra's `sentence-transformers` floor bumped
  from `<5.0` to `>=6.0,<7.0`, with an explicit `transformers>=5.5.0` floor
  (mirrored in `requirements.txt`) — see Fixed.
- CI gained a `test-hub` job (Postgres 16 service container,
  `hub/tests/` including `test_tenant_isolation.py`) alongside the existing
  core/dev jobs.

### Fixed
- `commontrace install --target cursor|generic-mcp` generated
  `commontrace.hub.mcp.json.example` was **not valid JSON**: the Hub tool
  list was interpolated into a JSON string field with an f-string template,
  leaking unescaped quotes into the file. It's now built with `json.dumps`,
  so it's guaranteed valid regardless of what the comment text says.
- `tests/test_attention_query.py` imported `numpy` unconditionally at module
  scope, so the whole test module (and therefore `pytest tests/`) failed to
  *collect* — not just skip — when the optional `attention` extra wasn't
  installed. It now uses `pytest.importorskip("numpy")`, matching the
  existing `sentence_transformers` skip, so the base install's test run
  (no `attention` extra) collects and passes cleanly.
- `[attention]`'s previous `sentence-transformers<5.0` cap transitively
  resolved a `transformers` version with 5 known RCE-class CVEs
  (PYSEC-2025-217, PYSEC-2026-2288/2289/2290) in checkpoint/config
  deserialization (found via `pip-audit`), fixed upstream in
  `transformers>=5.5.0`. Exploitability was already low here specifically —
  `memory/attention/query.py` only ever loads a hardcoded, trusted model
  name — but the new floor resolves to a version with zero known
  vulnerabilities per `pip-audit`.

## [2.0.0] - 2026-08-18

This release unifies the CommonTrace package and protocol under a single
version number and splits a previously coding-agent-specific shape into a
small, universal protocol core plus optional profiles. See
[`protocol/PROTOCOL.md`](protocol/PROTOCOL.md) (particularly §1 and §9) for
the full rationale.

### Added
- `protocol/PROTOCOL.md` — the canonical, implementation-independent
  CommonTrace Protocol spec: the `Trace` / `Lesson` object model (§3, §4), the
  Local/Hub store conformance tiers (§5), generalized roles (§6), the open
  taxonomy (§7), and the five pilot outcome metrics (§11).
- `protocol/schemas/trace.schema.json` and `protocol/schemas/lesson.schema.json`
  — universal, agent-agnostic JSON Schemas for `Trace` and `Lesson`, aligned
  1:1 with the production CommonTrace Hub's live trace object
  (`search_traces` / `get_trace` / `contribute_trace`). Mirrored into
  `commontrace/schemas/` so the installed package works without a repo
  checkout.
- `commontrace` CLI package (`commontrace/`) — a client-installable,
  agent-agnostic CLI (`pip install -e .`) with `init`, `install`, `capture`,
  `trace`, `lesson`, `query`, `index`, `bench`, `sync`, and `doctor`
  subcommands, and `commontrace install --target claude-code|cursor|devin|windsurf|generic-mcp`
  to wire a local store into a specific agent platform.
- `Trace.extensions` (namespaced under `Trace.profile`) as the mechanism for
  profile-specific fields that don't generalize across agent types (e.g. a
  git commit SHA for the code-review profile) — see PROTOCOL.md §9.
- `Trace.outcome` and the five pilot metrics (repeated-error rate, resolution
  rate, escalation rate, frustration rate, token/LLM-call cost) — see
  PROTOCOL.md §11 and `benchmark/pilot_metrics.py`.
- Support for five new agent types beyond `code`: `support`, `sales`, `hr`,
  `marketing`, `ops`, `custom`.

### Changed
- **`domain` went from a closed 7-value enum to an open vocabulary.** The
  code-review profile's original 7 values (`git-safety`, `cuda-gpu`,
  `refactor`, `testing`, `subagents`, `performance`, `other`) remain valid
  starter domains for `agent_type: code`; they are no longer the only
  values the protocol validates against (PROTOCOL.md §7).
  This is **not a breaking schema change**: `domain` was always a `string`
  field, and no enum constraint is removed from `trace.schema.json` or
  `lesson.schema.json` by this release — the constraint being lifted lived
  in the pre-2.0 coding-agent-specific implementation, not in a schema file
  present in this repo's history.
- **Profile-specific fields moved into `extensions`, namespaced under
  `profile`.** Fields like a commit SHA or a review verdict that only make
  sense for the code-review profile are no longer implied to belong on the
  universal `Trace`/`Lesson` core; a profile declares itself via
  `Trace.profile` and puts everything that doesn't generalize under
  `Trace.extensions` (PROTOCOL.md §9). A consumer that doesn't recognize a
  `profile` value can still safely read `title`, `context_text`,
  `solution_text`, `tags`, `agent_type` and ignore `extensions`.
- **Version unification.** Package version and protocol version were
  previously two different numbers (package `1.0.0`, protocol `1.1.0`).
  Both, along with `commontrace --version`, now report `2.0.0` identically
  (PROTOCOL.md §9).
- `SKILL.md`'s double-review pipeline (Alpha → A → B → Omega → Lambda) is now
  documented as *one* conformant profile — the "code-review profile,"
  versioned independently at v2.3 — rather than the only shape the protocol
  supports (PROTOCOL.md §1).
- `README.md` restructured around two entry points: the CLI (any agent type)
  and the code-review reference profile (`SKILL.md`), rather than only the
  latter.

### Breaking changes
- None at the schema level for existing data. Per PROTOCOL.md §9: *"a
  `Trace`/`Lesson` written under v1.x remains valid under 2.0.0."* This
  release is additive — new optional fields and an open (not newly
  restricted) `domain` — not a removal or retyping of any existing field.
- If a prior deployment's tooling relied on `domain` being restricted to
  exactly the 7 historical values (e.g. rejecting anything else), that
  external validation behavior is no longer enforced by the protocol itself
  now that the taxonomy is open; the values themselves still validate.

### Fixed
(Consolidated from the hardening passes folded into this release; see
`git log` for individual commits — "Bump to v2.0.0 and fix query/index
crashing without the attention extra," "Mega bug hunt: fix silent data
corruption, path traversal, and 15+ other confirmed bugs," "Second bug-hunt
pass: fix a business-critical metrics bug and complete the `---` delimiter
fix," "Phase 2 security hardening: frontmatter robustness, install safety,
dependency audit.")
- `commontrace query`/`commontrace index` no longer crash when the optional
  `[attention]` extra isn't installed.
- Path-traversal and other input-validation issues in lesson/trace writing.
- A business-critical metrics computation bug in `benchmark/measure_performance.py`.
- Frontmatter `---` delimiter parsing edge cases.
- Install-target file-overwrite and Hub-credential-in-git safety warnings
  (`commontrace install`).

### Removed
- The requirement that `domain` be one of exactly 7 fixed values — superseded
  by the open taxonomy in PROTOCOL.md §7 (see "Changed" above; not a schema
  removal, since no schema file in this repo ever encoded that enum).

[Unreleased]: https://github.com/denemlabs/commontrace-v2/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/denemlabs/commontrace-v2/releases/tag/v2.0.0
