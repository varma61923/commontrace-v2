# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **`query --experiment` silently did nothing on the semantic path.** Only
  the lexical branch honoured the holdout flags; the semantic branch
  forwarded just the query and `--top-k` to the reference script. A fleet
  with the `attention` extra installed - the recommended production setup -
  could run `--experiment` on every task forever while `commontrace
  experiment` reported "no holdout assignments recorded yet". The causal
  feature no-opped exactly where it was meant to run. The holdout is now
  applied in the parent to whatever the ranker returned, so ranking stays in
  one place and arm assignment stays in one place. `--agent-type`, which the
  semantic script genuinely cannot honour, is now announced rather than
  silently ignored.
- **`amend_trace` bypassed every write guard.** It skipped schema validation,
  size limits, the rate limiter, and quarantine - all of which
  `contribute_trace` enforces - making it the way around all of them:
  unbounded writes, and a title past the column width returning a hard 500
  instead of a clean rejection. All three guards now apply; verified live
  that an oversized amend is rejected as `invalid_request` and a spammy one
  is quarantined and invisible to search.
- **`import` wrote schema-invalid traces that `capture` refuses.** A bulk
  import is the likeliest source of malformed records - it is someone else's
  export - so accepting what `capture` rejects made the importer the one hole
  in the store's invariants, and a bad row was averaged into `bench --pilot`
  until an audit ran. Invalid rows are now rejected individually, named on
  stderr, and the command exits non-zero.
- **Holdout assignments were not de-duplicated on retry.** The log is
  append-only, so a retried task rewrote the same `(lesson, occasion)` pair;
  counting it twice inflated the arm and deflated the p-value, meaning a
  retry storm could manufacture significance. Duplicates are collapsed and
  the count is reported rather than hidden.
- **`minimum_detectable_effect(power=...)` was accepted and ignored** - both
  branches of a ternary were the 80% constant, so asking for 95% power
  silently returned the 80% answer and understated the sample size an
  experiment needs. Now computes the normal quantile by bisection (matching
  scipy to six decimals, still stdlib-only) and rejects an impossible power.
- **`release-quarantine` audited an empty reason.** It read
  `quarantine_reason` after the UPDATE had already synchronized it to `""`,
  so every audit row recorded `was=` - losing precisely the fact the row
  exists to preserve.
- **Alert text was interpolated into the HTML report unescaped**, bypassing
  the `html.escape` the same file applies to all other frontmatter text.
  Alert strings carry attacker-controllable values (a lesson's `importance`,
  its slug).
- **`commontrace bench` crashed on an episode with no `name`.** A malformed
  file must not take down a report about the whole corpus.
- **`install.sh` aborted on its own success path on bash < 4.4** (stock
  macOS): `${#ARR[@]}` on an empty array under `set -u` is an unbound
  variable. Replaced with a string accumulator, which has no such edge case.
- **Stale `benchmark/...` paths** in `install.sh`, `AGENTS.md`, and
  `benchmark/STATUS.md`, left behind when those scripts moved into the
  package. README had been updated; these had not.
- **`--dest` no longer loses to an exported `$COMMONTRACE_ROOT`.** `run_script`
  used `env.setdefault`, so a child script inherited the *old* env value and
  silently discarded an explicit `--dest`, inverting the precedence
  `paths.py` documents. The failure was invisible: `bench --pilot --dest B`
  rendered a normal-looking report full of store A's numbers. Affected
  `bench`, `bench --pilot`, `query`, and `index`.
- **`protocol/PROTOCOL.md` §2 contradicted itself three ways** — the heading
  said five stages, the table listed seven, and the diagram showed a
  different five with **Validate** missing and Inject renamed. The table is
  now normative at seven stages, the heading and diagram match it, and
  `install_cmd.py`'s restatement (which dropped Measure) was corrected.
  Losing Validate from a restatement of the pipeline drops the human
  approval gate, which is the protocol's central safety property.
- **`lesson list` and `trace list` crashed on a present-but-empty field.**
  `.get(k, default)` returns the default only when the key is *absent*;
  `status:` with no value parses to `None`, which has no `__format__` for a
  width spec. `lesson validate` diagnosed such a file cleanly while `list`
  died on it — the browsing command failing on exactly the file you are
  browsing to find. Both now render via a shared `_format.cell`.
- **Mistyped paths raised raw tracebacks.** `lesson validate /nope/x.md` and
  `trace validate <a directory>` reached `open()` unguarded. `frontmatter.read`
  now converts `OSError` into the existing `FrontmatterError`, so these
  report `[commontrace] error: cannot read …` and exit 1 like every other
  error path.
- **`capture` wrote traces that violate the shipped schema.** `--tokens-used -5`
  landed on disk and was only caught by a later `trace validate`, while
  `pilot_metrics` averaged the negative number into a customer-facing cost
  figure in the meantime. The instance is now validated before the file is
  written.
- **The YAML fallback parser disagreed with PyYAML on four numeric forms.**
  Its docstring claimed `7.0e3` resolved as a float "confirmed against
  yaml.safe_dump/safe_load"; PyYAML's YAML-1.1 resolver requires a *signed*
  exponent, so it is the string `'7.0e3'`. `0x1f`, `1_000`, and bare-leading-zero
  octal were also unhandled — `010` returned 10 where PyYAML returns 8, a
  plausible wrong number rather than an error. Both resolvers now follow
  PyYAML, and `tests/test_yaml_fallback.py` differential-tests them across
  300 generated documents, making good on a claim the docstring had been
  asserting without a test. The pre-existing test asserted the wrong
  behaviour and now reads ground truth from PyYAML instead.
- **`split_baseline` was O(n²).** `t not in baseline` is a full dict
  comparison per trace, correct only because `load_traces` happens to set
  `_path` on every dict — a load-bearing side effect of an unrelated line.
  Now compares identity.
- **Removed the committed `.devin/skills/commontrace/SKILL.md`.** It was the
  only install output checked into the tree, and `install --target devin`
  overwrites that exact path with the root `SKILL.md` — so the committed
  stub was whatever a Devin user saw until they ran install, at which point
  it was silently replaced by different content. It also still carried a
  `/justdoit` trigger. The installer's copy is canonical.
- **Deleted four stale pre-rename assets** (`justdoit_overall.{dot,png}`,
  `agent_orchestrateur.{dot,png}`) still carrying French labels and the
  string `/justdoit v2.3`. Nothing referenced them.
- **`SKILL.md`'s description was 1013 of 1024 permitted characters.** Eleven
  characters from silently failing to load. Trimmed to 779 by cutting the
  per-version changelog — release history is not what a model needs to decide
  whether a skill is relevant — and a test now enforces the limit.

### Added
- **`python -m hub.smoke` — post-deploy verification against a live server.**
  CI proves the code and the compose stack work; it cannot prove *your*
  deployment works — your TLS terminator, your managed Postgres, your
  ingress — and that gap is where deployments actually fail. Exercises all
  six MCP tools over real HTTP, confirms an invalid key is refused, and with
  `--other-api-key` confirms one tenant cannot read, vote on, or amend
  another's trace. A raw HTTP preflight runs first because the MCP client
  collapses a 401 into a generic internal error, so without it an operator
  cannot tell a rejected credential from a crashed server; each failure mode
  now yields one actionable sentence. Documented as DEPLOYMENT.md §12.
- **`compose-stack` CI job — the deployment path, end to end.** Brings up the
  documented stack, waits on `/readyz`, asserts migrations created every
  table, provisions two orgs through the operator CLI, drives the running
  server over real HTTP (all six tools, 401 without a key, cross-tenant reads
  refused), restarts the app and checks data survived, and asserts the logs
  are structured JSON containing no API key or database password. Every other
  job tested a piece; this is the only one proving the pieces compose.
- **Readiness healthcheck on the `hub` compose service.** It probes `/readyz`
  rather than `/healthz` — readiness checks the database, which is what "can
  this container serve a request" actually depends on; the liveness endpoint
  would report healthy while every call failed. Implemented with `python`
  rather than `curl`, which the runtime image deliberately does not carry.
- `validate.assert_supported_schema()` — this validator implements a
  deliberate subset of JSON Schema, and an unsupported keyword was previously
  ignored in silence. Adding `pattern` or `maxLength` to a schema would have
  meant the constraint was enforced nowhere while documents still reported
  valid. Unknown keywords now raise, and both shipped schemas are checked.
- A test asserting `protocol/schemas/` and `commontrace/schemas/` stay
  byte-identical. They are committed twice so a bare `pip install` can
  validate, and nothing had been keeping them equal.
- Coverage for `lesson list` / `trace list`, which had none at all.
- **`commontrace bench` and `bench --pilot` now work from a plain
  `pip install`.** Both reference scripts lived only in the repo checkout, so a
  customer could install the product and still be unable to compute their own
  pilot metrics — the one number they most need, and the whole point of the
  before/after story. They now ship inside the wheel at
  `commontrace/reference/` (declared as `package-data`; the directory has no
  `__init__.py` because they are executed as subprocesses, not imported).
  Script resolution checks the store root and cwd first, so a contributor's
  edited copy still wins. Verified by installing the built wheel into a clean
  venv and running both commands from a directory with no checkout anywhere
  near it. `doctor` now reports a missing benchmark script as a real failure
  (damaged install) rather than the expected-for-clients `[INFO]`.
- **`capture` and `lesson new` now inherit the store's `agent_type`.** `init
  --agent-type support` stamps the type into `memory/INDEX.md`, but both
  commands hard-defaulted to `code`, so a support/sales/ops fleet silently
  mislabeled every record unless the operator repeated `--agent-type` on every
  invocation. Wrong, and invisible until a later filter mysteriously returned
  nothing. An explicit flag still overrides.
- **The generated Hub MCP config could not connect.** `commontrace install`
  wrote the stdio shape (`command`/`args`/`env`) for a server that speaks
  streamable-HTTP — there was nowhere to put the endpoint or the bearer token,
  so anyone pasting the template simply failed to attach. It now emits the
  http shape (`type`/`url`/`headers`), verified by connecting to a live Hub
  using only the generated file. The regression test had been asserting the
  broken shape and was updated.
- **`import` accepts the protocol's own field names.** `context_text` /
  `solution_text` are what `sync --pull` writes and `search_traces` returns,
  yet the importer required `--context-field` flags to rename them into the
  names we ourselves emit — so the product could not round-trip its own
  export. Both spellings now work; an explicit mapping still wins, and a
  genuinely missing field names both accepted spellings.
- **`hub.manage` reports operator mistakes as errors, not tracebacks.** A bad
  day count or an unknown org id raised a raw `ValueError` traceback from the
  production operator CLI; these now print `error: ...` and exit 2.

### Added
- **Causal effect measurement via randomized holdout** (`commontrace
  experiment`, `commontrace query --experiment`). Every lesson-value number
  this project reported until now — including `reliability`'s `lift` — is
  correlational, and the confound is structural: a lesson is retrieved
  *because* the situation matched its activation condition, so the occasions
  where it fired differ systematically from the ones where it did not. That
  bias does not shrink with more data. Verified in simulation against a
  known ground truth: correlational scoring labeled a genuinely helpful
  lesson HARMFUL (−13.7%) and a useless one RELIABLE (+20.0%) — both exactly
  backwards; the holdout recovered the truth in each case. Both are now
  regression tests.
  - `query --experiment --occasion-id <id>` withholds a lesson from a random
    ~10% of the occasions where it was *eligible* and appends the arm
    assignment to `memory/holdout_log.jsonl`; `experiment` joins those arms
    to recorded episode/trace outcomes and reports a causal effect with a
    95% CI and a p-value.
  - Assignment is a deterministic hash of `(lesson, occasion, salt)`: no
    stored state, exactly reproducible when a result is disputed later, and
    stable under retries so an occasion cannot flip arms by being processed
    twice. It is independent per lesson, which is what makes two lessons
    that always co-fire separable at all — no observational method can do
    that.
  - Verdicts are HELPS / HURTS / NO_MEASURABLE_EFFECT / UNDERPOWERED, with
    the last deliberately separate so "not enough data yet" is never read as
    "tested and found useless". Significance is Benjamini-Hochberg-corrected
    across tested lessons; underpowered comparisons are excluded from the
    correction rather than inflating `m`. Null results quote their minimum
    detectable effect. `--strict` exits non-zero if any lesson significantly
    hurts outcomes, so a regression can gate CI.
  - Cost is bounded and stated rather than hidden: in the worst case the
    lesson would have helped and 1 occasion in 10 loses that help.
    `--holdout-rate 0` opts out entirely. Statistics are stdlib-only
    (`math.erf`), so the core install stays PyYAML-only.
- **`commontrace reliability` now states that `lift` is correlational** and
  points at `commontrace experiment`. The report puts `lift` in a table
  beside a HARMFUL verdict; without the caveat a reader takes it as a causal
  claim, and it is not one. No metric, formula, or threshold changed — only
  what the report says about itself.
- **Production deployment artifacts.** `Dockerfile` (multi-stage, non-root,
  no build toolchain in the runtime layer), `docker-compose.yml` (with
  migrations as a one-shot service the app waits on, so replicas can't race
  the same DDL), `.dockerignore`, and `hub/DEPLOYMENT.md` covering probes,
  scaling, backup/restore, and a pre-client security checklist. The image
  could not be built where it was authored (no Docker daemon), so CI gained
  a `docker-build` job — now passing — that builds it, starts the container,
  and asserts `/healthz` serves while `/readyz` returns 503 with no database.
  The compose stack is still not exercised by CI, and neither has had a
  production-like rehearsal.
- **Observability** (`hub/observability.py`): JSON logs on stdout, a
  request-correlation id (honoring an inbound `X-Request-ID`, echoed back in
  the response) on every log line, and one structured line per request with
  method/path/status/duration — deliberately never query strings or bodies,
  which carry customer content.
- **`/readyz`, split from `/healthz`.** `/healthz` (liveness) answers "is
  this process alive" and does **not** touch the database on purpose;
  `/readyz` (readiness) runs `SELECT 1` and returns 503 when Postgres is
  unreachable. Previously a single `/healthz` returned 200 even with the
  database down, so a load balancer kept routing to instances that could not
  serve a single request.
- **Audit log** (`hub/audit.py`, `audit_log` table): every MCP write and
  every `hub/manage.py` admin command is recorded. Rows deliberately survive
  an org purge (`org_id` is not a cascading FK — the purge is exactly the
  event a trail must retain) and carry only bounded metadata, never trace
  content. Viewable with `python -m hub.manage audit-log [org_id]`.
- **API-key expiry.** `issue-key <org_id> [days]`; expired keys are rejected
  at verification with no revocation job needed, and are indistinguishable
  from invalid ones. Rotation carries the expiry *policy* forward, so a
  90-day key never silently rotates into a never-expiring one.
- **Search pagination.** `search_traces` takes `limit`/`offset` (clamped to
  `MAX_SEARCH_LIMIT`) and returns `has_more`. It previously hard-capped at 50
  with no offset, so a client could not reach result 51 at all.
- **Client resilience.** `commontrace/hub_client.py` now sets a finite
  request timeout (it had none, so a stalled Hub hung `sync` forever) and
  retries transport failures with exponential backoff — never retrying auth
  or validation failures, which cannot succeed on a second attempt.
- Connection-pool sizing, graceful shutdown (the engine is now disposed on
  exit rather than dropping pooled connections), and a CI step that applies
  every migration to an empty database plus `alembic check` — migrations
  were previously never exercised by CI at all.

- **Hub admin/monitoring commands** (`hub/manage.py`): `stats` (org/key/
  trace/vote counts, mean trust), `list-quarantined [org_id]` (the
  abuse-control review queue), `release-quarantine <trace_id>`, and —
  closing a gap `DATA_RETENTION.md` previously flagged as entirely
  unimplemented — `purge-trace <trace_id>` / `purge-org <org_id>`
  (permanent, operator-CLI-only deletion; cleans up `trace_relations` rows
  that FK cascades don't reach since `related_trace_id` isn't a foreign
  key). There is no web admin panel; this CLI is deliberately the whole
  admin surface for now — a dashboard needs its own cross-org admin auth
  model, a separate decision from the org-scoped API keys `hub/auth.py`
  issues. Every `hub/manage.py` command now takes an optional
  `session_factory` for dependency injection (tests inject a fixture's;
  the CLI defaults to building one from `HubConfig.from_env()`), replacing
  what would otherwise need module-global monkeypatching to test.
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
- **`search_traces` matching is full-text, not substring — a visible
  behavior change, not a transparent optimization.** The old
  `ILIKE '%query%'` could not use any index (a leading wildcard defeats
  B-tree prefix matching), so every search sequentially scanned the org's
  traces. Matching now goes through a `GENERATED ... STORED` tsvector column
  and a GIN index. Measured on 50k traces in one org with a selective query:
  **113 ms sequential scan → 9 ms index scan**, and the cost stops growing
  linearly with the store. The trade cuts both ways: "deploy" now also
  matches "deployed" (stemming), but "ploy" no longer matches "deploy".
  Results with a query are ordered by relevance then recency.
- **`search_traces` returns a dict** (`{"traces", "limit", "offset",
  "has_more"}`) rather than a bare list, to carry pagination state.
- **Fixed an N+1 in trace hydration.** Votes and relations were fetched
  per trace, so a 50-result search issued 101 queries; they are now
  batch-loaded in 2 queries regardless of result count.
- `hub/tests/conftest.py` drops and recreates the schema per session:
  `create_all` never ALTERs existing tables, so a test database left on an
  older revision silently kept stale columns.
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
