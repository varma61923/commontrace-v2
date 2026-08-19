# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
- `ruff` added to the `dev` optional-dependency group.
- README install-target quick-reference and file-layout table point at the new
  support matrix instead of asserting untested platform behavior.
- Documentation no longer implies `pip install commontrace` (bare, from PyPI)
  works today; `pip install -e .` from a repo checkout is the only currently
  verified install path, and PyPI publication is called out as a future step
  (see `protocol/PROTOCOL.md` §8 and `README.md`).

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
