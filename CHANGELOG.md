# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A read-only operator console at `/admin`**, served by the Hub itself and
  off unless `HUB_ADMIN_TOKEN` is set. Every organization against its plan,
  per-key state and expiry, quarantined traces, retrieval miss rate, recent
  audited actions, and the Knowledge Base review queue — on one page.

  The reason it exists is not convenience. Every serious defect found in this
  branch's audit was *invisible*: placeholder lessons counted as coverage, a
  bulk sync failing silently, an acceptance check printing "returned our
  trace" on a passing run. They were invisible because the only way to see
  this system's state was to run a command and read text, and nobody runs a
  command for a question they have not thought to ask yet.

  **Read-only is a security decision, not a missing feature.** Before this,
  the Hub had no browser-facing surface at all — no cookies, no sessions,
  nothing for a CSRF to target, and `X-Frame-Options: DENY` set with a
  comment saying there was nothing browser-rendered to protect. The operator
  actions worth putting in a UI are also the worst ones to get wrong:
  `purge-org` irreversibly destroys one customer's entire history, and
  `issue-key` would render a raw credential into browser history and any
  screenshot of it. So the console renders state and, for anything that
  changes state, shows the exact `hub.manage` command — the operator still
  sees everything in one place, but the last keystroke happens in a terminal
  that already prompts for confirmation and writes an audit row. Making it
  read-write is a deliberate second phase with its own security work, not a
  flag flip.

  Three properties are asserted rather than asserted-to:

  - **Absent unless configured.** With no token, the routes are never
    registered — an unauthenticated prober gets a 404 from the router, not a
    401 from a handler. Same "absent, not merely refused" treatment
    `HUB_COMMONS_ENABLED` gives the Knowledge Base tools.
  - **Escaped.** The console renders content from every tenant into the one
    browser session with cross-tenant visibility, so a trace title is stored
    XSS waiting to happen. Every value goes through `h()`; a test asserts a
    trace titled `<script>alert('pwn')</script>` renders inert.
  - **Structurally read-only.** A test asserts no page contains a `<form>`,
    a `<button>`, a POST target, or a `fetch(` — the guarantee is checked,
    not just documented.

  Authentication is HTTP Basic (username ignored, password compared with
  `hmac.compare_digest`), rate limited by client address *before* the
  credential is checked, and no unauthenticated request reaches the database.

### Fixed

- **`commontrace sync --push-traces` could not complete against a
  default-configured Hub, and reported the failure as a network outage.**
  Measured on a 46-trace store: **0 of 46 traces pushed**, 62 of 100
  requests refused with HTTP 429, and every one reported as `could not
  reach the Hub ... unhandled errors in a TaskGroup (1 sub-exception)`.
  After this change the same store pushes **46 of 46 in 23s with zero
  429s**. Four independent defects, each reproduced against a live Hub:

  - *Five HTTP requests per logical tool call.* Every `_call_tool` stood up
    a whole MCP session of its own -- connect, `initialize`, the
    initialized notification, `tools/call`, terminate -- and the Hub's
    limiter counts HTTP requests, not tool calls. A batch now holds **one**
    session open (`HubSession`), opened lazily so an already-up-to-date
    sync makes no connection at all.
  - *The retry layer never ran.* The MCP SDK drives its transport inside an
    anyio task group, so every transport error arrived wrapped -- twice --
    in an `ExceptionGroup` whose only text is "unhandled errors in a
    TaskGroup". Classification read that wrapper, so a genuine refused
    connection was judged **not** retryable and tried exactly once, and
    `--max-attempts` was a no-op flag. Errors are now classified against
    the flattened exception tree.
  - *429 was not handled at all.* It is the one client error a later
    attempt can succeed at. It is now retried under its own attempt budget,
    with a shared `_RateLimitGate` that paces the **whole batch** (per-call
    backoff alone just leaves the other workers stampeding a limiter that
    has already said no) using additive-increase/multiplicative-decrease
    against the server's own `Retry-After`.
  - *Every failure claimed to be a network outage, after a fabricated
    number of attempts.* A rejected API key, a rate limit and a real outage
    were indistinguishable, and "after 3 attempt(s)" was the configured
    maximum printed unconditionally -- a failure that gave up after one
    attempt still claimed three. Failures now name themselves
    (`HubAuthError`, `HubRateLimited`, `HubConfigurationError`,
    `HubToolError`), report the real attempt count, and quote the actual
    underlying exception.

- **Unedited scaffolding could become an active, injected, published
  lesson.** Reproduced end to end on a real store: `commontrace distill`
  writes a candidate whose Rule, How-to-apply, Counter-examples,
  `applies_when` and `do_not_apply_when` are all `TODO: ...`; `lesson
  approve` activated it; `lesson validate` called it "1/1 lessons valid";
  `query` returned it as the top hit; `taxonomy` reported its source
  pattern as **covered**; `pilot` reported **"Gaps: 0"**; and `sync --push`
  published it to the whole fleet. An agent injects whatever it is given,
  so this is the failure this codebase names elsewhere as "context
  poisoning with this product's name on it" -- and every report the
  customer reads described it as coverage.

  `approve` now names the unfilled sections and refuses (`--force`
  overrides and says so), `validate` fails an *active* lesson in that
  state, `taxonomy`/`pilot` do not count it as coverage, and `sync --push`
  will not publish it. `lesson new` also scaffolds at `status: review`
  rather than `active` -- it wrote a lesson that was live, retrievable and
  publishable over a body that was still entirely template text, bypassing
  the Validator gate the protocol defines.

- **`sync --pull` followed by `sync --push-traces` pushed the Hub's own
  traces back to it.** Pulled records are written into the local traces
  directory as `hub_<slug>_<id>.md` with `hub_trace_id` already set, so the
  push path saw each as "on the Hub, no recorded fingerprint" and amended
  the Hub's trace with a round-tripped copy of itself -- 46 captured traces
  became 68 push candidates after one pull, each spurious amend spending a
  write-rate-limit token and a plan storage slot.

- **The anti-brute-force limiter throttled legitimate clients hardest.**
  The auth-attempt limiter exists to bound the Argon2 CPU an
  unauthenticated source can force, but it charged every request --
  successful ones included. A bulk push is hundreds of *successful*
  authentications from one address against a 60/min budget, so the
  brute-force defense, not the per-org fair-use limiter, was the binding
  constraint on this product's own documented onboarding. The token is now
  refunded when the credential verifies; a source presenting bad keys is
  throttled exactly as before.

- **`hub/bench_scaling.py` crashed while printing its own results.**
  `growth_factor` is `None` whenever the smallest corpus measured 0ms --
  the ordinary case for a fast read path -- and formatting `None` with
  `:>6.1f` raises `TypeError`, after every measurement had been taken and
  thrown away. The sibling `exponent` on the same row was already guarded.

- **`commontrace doctor` printed affirmative labels for negative results**,
  e.g. `[INFO] attention extra installed (numpy + sentence-transformers) -
  optional; install with pip install ...` -- a line asserting the extra is
  installed and then telling you to install it. Labels are now neutral.

### Added

- **`--threshold-lexical`, `--threshold-freshness` and
  `--threshold-composite` are implemented.** They were parsed, forwarded by
  `commontrace bench`, and read by nothing: a fleet could set a quality
  gate, watch it never fire, and conclude quality was fine. They now
  compute real metrics -- lexical near-duplicate detection (no optional
  dependency, unlike `--threshold-semantic`), the fraction of lessons hit
  in the last 90 days, and a combined health score that names its own
  components -- each raising a real alert and, under `--strict`, a real
  non-zero exit. All three stay opt-in, so a run passing none of them
  produces exactly the report it did before.

- **`GET /metrics`** (Prometheus text format): requests by method/route/
  status, summed duration per route, and refusals per limiter. Rate
  limiting was otherwise invisible until a customer complained.
  Deliberately carries no org id, key prefix or query text -- a scrape
  endpoint is a different trust boundary from an authenticated tool call --
  and buckets unknown paths to `other` so a caller cannot inflate label
  cardinality.

- **Every numeric Hub setting is range-checked at startup.** `HUB_PORT=99999`
  died in uvicorn's bind, `HUB_DB_POOL_SIZE=-1` in SQLAlchemy on first
  query, and `HUB_MAX_TITLE_CHARS=-5` rejected every `contribute_trace`
  with nothing anywhere saying why. A bad value now refuses to start and
  names the variable and the bound.

- **`Retry-After` on every rate-limit refusal**, HTTP and tool-level alike,
  plus a one-time notice from `sync` explaining that a large push is pacing
  itself -- a correct slow push read as a hang.

### Changed

- **`HUB_RATE_LIMIT_PER_MINUTE` default raised from 20 to 120** (burst 5 to
  30). The old default could not serve this product's own documented
  onboarding: at 20/min a 46-trace store took over two minutes and a
  1,000-trace import the better part of an hour. Two writes per second per
  org still protects the shared Postgres and still bounds a runaway agent.

- **The Hub's rate limiter caps how many keys it tracks.** The idle sweep
  evicts nothing for an hour, and the client-address-keyed limiters are
  keyed on something the peer chooses (any address out of an IPv6 /64), so
  an unauthenticated flood could grow process memory without bound via the
  limiter meant to prevent exactly that.

- **An MCP session-teardown `DELETE` is no longer charged to the read
  limiter.** It runs no tool and reads no row, and refusing it made an
  otherwise successful command print `Session termination failed: 429`.


### Added
- **The measurement loop is now reachable from where agents actually
  are**: an optional `occasion_id` on `search_traces`, the Hub's full tool
  surface in the generated MCP config, and holdout instructions in **both**
  skills `commontrace install` can write -- the repo's own `SKILL.md`
  (used whenever installing from a checkout, which is the common case) and
  the pointer skill that stands in when no `SKILL.md` is found.

  Three layers of the same gap, found by checking rather than assuming.
  The randomized holdout existed on the Hub (§19) and in the CLI, and an
  agent wired up by `commontrace install` was told about none of it -- so
  it would never be called and the experiment would never run. The
  generated MCP config also still advertised the **original six** tools
  while the Hub had grown to eighteen, so twelve tools (every measurement
  tool among them) were simply never discovered by anyone reading it.
  That drift is silent by construction: the file stays valid JSON, still
  connects, and works fine for the six it names.

  `search_traces(query, occasion_id=...)` now returns a `holdout` block
  naming which results must not be used on that occasion, and records the
  arms. This is friction reduction with a point: the local tier makes a
  holdout one flag (`query --experiment`) because retrieval itself
  withholds and logs, while the Hub needed two extra calls wrapped around
  every retrieval -- a rewrite of an agent's loop rather than an opt-in.
  Every trace is still returned, so `search_traces`' contract is unchanged
  and a caller ignoring the block behaves exactly as before; omitting
  `occasion_id`, or running with no experiment, changes nothing at all.

  In `SKILL.md` the instruction lands in Phase 0, the retrieval phase --
  the only point that knows which lessons were *eligible*, and
  eligibility is what makes the later comparison causal rather than
  confounded. Its required output block gains a "Withheld by the
  holdout" slot, so an agent honouring the experiment can say so and a
  reviewer can tell a withheld lesson from one that simply did not
  match.

  `hub/tests/test_install_template_surface.py` pins the advertised tool
  list against `hub/smoke.py` and pins that both skills still
  teach the rule that fails silently -- using a withheld trace does not
  raise, it just biases the effect toward zero, so an agent has to be told.

- **`commontrace prove`**, the client path to the Hub's measurement tools
  (`prove outcomes` / `prove assign` / `prove record`, plus
  `hub_client.fleet_outcomes` / `holdout_assign` /
  `record_occasion_outcome`).

  The three tools below were added to the Hub and wired to nothing a
  customer could reach: no `hub_client` function, no CLI command. A fleet
  would have had to hand-write MCP calls to run the experiment STRATEGY.md
  §13.2 calls the cheapest falsifier available. Building an instrument and
  leaving it where the users are not is the same failure §19 corrected for
  the Hub, committed again at the client boundary in the same session.

  `prove outcomes` prints the causal result FIRST when there is one, and
  the ordering is the claim: a reader who meets the before/after table
  first will quote it, and that is the number that dies to "what else
  changed that quarter?". When no experiment is running it says so
  explicitly, so the observational table below can never be mistaken for a
  causal one. `prove record` requires an explicit `--succeeded` or
  `--failed` -- defaulting either way would quietly bias every hurried
  report.

- **The randomized holdout, in the Hub** (`holdout_assign` /
  `record_occasion_outcome` MCP tools, `hub/manage.py start-experiment` /
  `experiment` / `stop-experiment`, `HoldoutObservation`). The only
  structure here that supports a CAUSAL claim: two arms of the same fleet
  in the same window, differing only by whether the memory was injected,
  so "what else changed that quarter?" has an answer.

  This closes the largest gap this repo has recorded. STRATEGY.md §11.3
  names causally-measured memory as the entire moat and §13.2 calls
  running it "the cheapest falsifier in the document" and says to run it
  first -- and both were true only of `commontrace/experiment.py`, which
  works against a **local file store**. The Hub had no notion of a holdout
  at all: no assignment, no arms, no observations. So a Hub customer --
  which is to say the product -- could obtain no causal number of any
  kind, and §13.2's most gating falsifier could not be run on paying
  customers without asking them to abandon the Hub for local files.

  Validated against seeded ground truth over 700 occasions: a +30% lesson
  recovered at +29.2% (CI [+21.7%, +36.7%], HELPS), a -25% lesson at
  -24.5% (CI [-31.8%, -17.3%], HURTS), and a 0% lesson correctly reported
  as no measurable effect with its minimum detectable effect (~13%) rather
  than as evidence of absence. Every interval covers the true value.

  `HURTS` is the verdict only this can produce: a lesson retrieved often
  *because* it fires on the hardest tasks scores well on every
  correlational signal here -- retrievals, trust, `commons_hits` -- and may
  be making outcomes worse. No amount of observation separates those two
  stories.

  Four properties exist because their absence fails silently rather than
  loudly: assignment is a deterministic hash of (salt, trace, occasion) so
  a retry cannot move an occasion between arms; the salt is per-org and
  never edited, so restarting starts a *new* experiment and two
  randomizations are never pooled; eligibility is a row's existence rather
  than a client-reported flag; and unresolved observations are excluded
  rather than counted as failures, so the arm whose agents crash more is
  not penalised for it. Analysis is `commontrace.experiment.analyze`
  imported unchanged -- a drifted copy would randomize the same lesson two
  ways across a fleet running both tiers and silently compare two
  mixtures.

  **The cost is real and stated:** the withheld fraction gets a worse
  product on purpose. No migration or default turns it on (`holdout_rate`
  defaults to 0); an operator decides per org and the decision is audited.
  Migration `b7e4c91d2a08`. Brings the MCP surface to 18 tools. 26 new
  tests (`hub/tests/test_holdout.py`).

- **A measured answer to STRATEGY.md §13.2's weakest link**
  (`hub/bench_scaling.py`, results in `hub/SCALING.md`). §13.2 lists five
  links the business case rests on and marks exactly one "unmeasured, and
  the weakest link nobody has looked at": *value compounds within a
  customer faster than it costs to serve them*, with the falsifier "if
  serving cost grows with corpus size faster than value does, this is a
  services business wearing infrastructure clothes." Nobody had run it.

  Measured across a 64x corpus range (1,000 -> 64,000 traces in one org,
  median of 9 runs, fitted as `latency ~ size**alpha` by least squares on
  log-log axes): **no read path grows linearly with a customer's own
  corpus.** A selective `search_traces` is 0.19 -- 64x the history costs
  2.2x the query -- tag search is flat, and the worst operator-facing
  report is 0.74. The cost side of that falsifier does not fire.

  Stated limits, because the table is the least important part: this is
  the cost half only (value per query needs real customers, not synthetic
  rows); every number is a single query against an idle database, so
  concurrency is a separate unmade measurement; and only the exponents
  transfer, never the milliseconds.

- **Fleet outcome measurement in the Hub** (`fleet_outcomes` MCP tool,
  `python -m hub.manage outcomes [org_id]`, `hub/outcomes.py`).
  `Trace.outcome` has carried the five business-outcome fields --
  `resolved`, `escalated`, `repeated_error`, `frustration_signal`,
  token/call cost -- and the `baseline` before/after flag since the schema
  was written, and every `contribute_trace` writes them. The Hub read that
  column in exactly two places (copying it onto the wire projection,
  carrying it forward on amend) and computed nothing from it, so a
  deployment holding a year of a fleet's outcome history could not answer
  whether the product was working. It can now: resolution, repeated-error,
  escalation and frustration rates for the baseline window versus
  everything since, with deltas, 95% confidence intervals, p-values, and
  mean token/call cost.

  This closes a gap under two load-bearing claims rather than adding a
  report. STRATEGY.md §11.3 names measured effect on the customer's own
  data as the entire moat, and §11.5 names measured resolution-rate
  improvement as the only pricing denominator this product can defend.
  Both were claims about a number nothing in the Hub could compute -- the
  client CLI could compute a local version for a customer who thought to
  run it; the service could not. For an operator, `manage.py outcomes`
  with no org is a leading churn indicator where `usage`/`revenue` are
  lagging ones.

  **It is a before/after comparison, not a causal estimate, and nothing
  in it is allowed to imply otherwise.** `baseline` marks a time window,
  so a model upgrade or a shift in task mix is confounded with this
  product's contribution; `outcomes.OBSERVATIONAL_CAVEAT` rides on every
  response and every rendering, and points at
  `commontrace/experiment.py`'s randomized holdout as the design that can
  support a causal claim. Three further properties exist specifically to
  keep the number quotable, and all three make the conclusion weaker: a
  Benjamini-Hochberg correction across the four metrics (so a lucky one
  out of four does not get quoted); a minimum detectable effect on every
  inconclusive row (so a small sample cannot read as "no effect"); and
  `worsened` as a first-class verdict reported at the same prominence as
  a win, never sorted below one. Where a row's uncorrected 95% CI
  excludes zero but its corrected verdict is `no change`, the row
  explains the difference rather than leaving a reader to conclude one of
  the numbers is broken.

  The statistics are imported from `commontrace/experiment.py`
  (`two_proportion_test`, `diff_confidence_interval`,
  `benjamini_hochberg`, `minimum_detectable_effect` -- pure stdlib, so no
  new Hub dependency), not reimplemented: the same reasoning
  `hub/commons.py` gives for importing the client's MinHash, except that
  here a drifted near-copy would cause *disagreement* between the
  customer's own tooling and the operator's report about the same fleet,
  which finishes the number as evidence regardless of which was right.

  Org-scoped like every other read in `hub/crud.py`, unmetered (it reads
  the caller's own traces, and charging a customer to ask whether the
  product works would be the worst possible place for a meter), and
  quarantined traces are excluded. Brings the MCP surface to 16 tools.
  24 new tests (`hub/tests/test_fleet_outcomes.py`).

- **Knowledge Base entry standing, and the operator queue that acts on
  it.** Seeding and community submissions both answer how content gets
  into the Knowledge Base; nothing answered what happens when an entry
  stops being true, and a curated corpus that only grows is one that
  decays. Two signals the system already collected and then discarded now
  drive that: `Trace.trust` (computed from every vote on an entry, and
  previously read by nothing but a tie-break) and `Vote.feedback_tag`
  (`outdated`/`wrong`/`security_concern`, previously consulted nowhere).
  `hub/commons.py:entry_standing` turns them into one label --
  `disputed` / `stale` / `established` / `unproven` -- carried on every
  Knowledge Base projection.

  What acts on it: `commons_overlap` no longer counts a disputed entry as
  coverage (a wrong answer is not a solved failure) and returns it
  separately under `disputed_matches` instead, so the coverage figure
  never moves without the caller being able to see why; `commons_search`
  still returns disputed entries, ranked last and labelled, because for
  lookup a contested answer beats no answer. **No vote count withdraws
  anything.** The strongest automatic consequence is a smaller coverage
  claim and a worse rank -- both of which make this product's own numbers
  more conservative, never less -- and
  `hub/tests/test_kb_standing.py:TestVotesNeverRetract` pins that as a
  property rather than an intention.

  `hub/manage.py kb-review` is the operator work list: security-flagged
  entries first (a single `security_concern` vote is the one signal acted
  on at n=1, and what it does is raise priority, not remove anything),
  then disputed, then past their review date, then never-matched, each
  bucket ordered by traffic affected -- so review cost tracks the error
  rate rather than the corpus size, which is what makes operator curation
  scale past what anyone could re-read. `kb-retract <trace_id> [reason]`
  withdraws an entry from all three Knowledge Base read paths at once
  (via the new shared `hub/crud.py:commons_visible()` filter) while
  keeping its row, votes, and hit history; `kb-restore` reverses it. This
  closes the gap DATA_RETENTION.md flagged as open ("there is no CLI
  command to correct or remove a single Knowledge Base entry after
  commons-seed has loaded it, short of a direct database operation");
  correcting an entry *in place* remains unbuilt and is now the narrower
  open item there.

  New: `Trace.commons_votes` / `commons_review_after` /
  `commons_retracted_at` / `commons_retraction_reason` (migration
  `8f2b40c17ade`, which backfills `commons_votes` from the existing
  `votes` table and narrows the partial commons index to match the new
  filter), `commons.entry_standing` / `counts_as_coverage`,
  `crud.retract_kb_entry` / `restore_kb_entry` / `kb_review_queue`, a
  `review_after` field on `commons-seed`'s JSONL input so version-pinned
  substrate knowledge can declare its own expiry at authoring time,
  standing warnings in `commontrace commons ask` output and a
  disputed-matches section in `commons report`, and 70+ new tests
  (`hub/tests/test_kb_standing.py`, `tests/test_commons_cmd.py`).

  No new MCP tools: the customer-facing input (`vote_trace`) and output
  (`commons_overlap`/`commons_search`) already existed; what changed is
  that the input is now read and the output now says what it means.

- **Self-service deletion.** An org's own API key can now delete its own
  data without operator/DB-access trust: `delete_trace` removes one trace
  (and its full amendment chain) immediately, and
  `request_account_deletion` / `confirm_account_deletion` /
  `cancel_account_deletion` remove the entire organization -- every trace,
  vote, api key, and Knowledge Base submission. Whole-account deletion is
  deliberately two calls, not one: `request_account_deletion` deletes
  nothing by itself, only returning a one-time confirmation token, and
  `confirm_account_deletion` refuses to run until a mandatory delay
  (`crud.DELETION_GRACE_SECONDS`, 5 minutes) has elapsed since the
  request -- long enough for an operator watching the audit log (every
  request is recorded there) to `revoke-key` a compromised credential
  first. This closes the gap DATA_RETENTION.md previously flagged as open
  ("an org cannot delete its own data via its own API key") and answers
  the authorization question hub/README.md's Operator CLI section had
  left unresolved ("should a single compromised key be able to wipe an
  org's entire trace history with no confirmation step?") with no.

  New: `Organization.deletion_token_hash`/`deletion_requested_at`/
  `deletion_expires_at`, `hub/crud.py:delete_trace` /
  `request_org_deletion` / `cancel_org_deletion` / `confirm_org_deletion`
  / `amendment_chain` (the amendment-chain walk, shared with
  `hub/manage.py:purge_trace` rather than duplicated), four new MCP tools,
  `commontrace account delete-trace` / `request-deletion` /
  `cancel-deletion` / `confirm-deletion` CLI subcommands, and 40+ new
  tests (`hub/tests/test_self_service_deletion.py`,
  `tests/test_account_cmd.py`) covering the tenant-isolation, grace-period,
  and token-matching invariants against a real Postgres instance.

- **A reviewed community-submission channel for the Knowledge Base**
  (`submit_kb_entry` / `list_my_kb_submissions` MCP tools, `commontrace
  commons submit` / `commons submissions` CLI, `hub/manage.py
  list-submissions` / `approve-submission` / `reject-submission`
  operator commands). An org may propose a Knowledge Base entry, but
  nothing is published by that call: it writes to a new
  `KnowledgeBaseSubmission` table that `commons_overlap`/`commons_search`
  never read, and stays invisible to every other org -- submitter
  included, as coverage -- until an operator's own `approve-submission`
  action accepts it. Approval publishes it as a new
  `Trace(commons_source='seed')` owned by the operator (never the
  submitter, exactly like `commons-seed`) and permanently raises the
  submitting org's Knowledge Base query allowance
  (`Organization.bonus_commons_queries`, `plans.SUBMISSION_ACCEPTANCE_CREDIT`
  by default); rejection awards nothing.

  This reopens a growth channel without reopening the adverse-selection
  problem the retired org-to-org design had (STRATEGY.md §3): crediting
  the act of *sharing* rewards volume, so an org keeps its best lessons
  and submits filler to farm allowance. Crediting *acceptance* rewards
  quality instead, since filler gets rejected and earns nothing -- the
  same discipline a Stack Overflow answer or a wiki edit is under, not a
  reason to trust a customer with a raw sharing switch. See
  `hub/models.py:KnowledgeBaseSubmission`, `hub/plans.py` "why
  bonus_commons_queries is not the same mistake twice", and STRATEGY.md
  §15 for the reasoning and for why this is a labor multiplier on
  operator review throughput, not a network effect.

  `hub/manage.py kb-stats` now also reports the submission funnel
  (pending/approved/rejected, distinct submitting orgs) alongside its
  existing content-quality numbers.

### Changed
- **The commons is no longer org-to-org. It is a single, optional,
  operator-curated Knowledge Base.** The previous design let one org opt a
  trace into a shared corpus other orgs' queries could match against
  (`share_trace`/`unshare_trace`, `commontrace commons contribute`). That
  design is retired: it does not make sense for orgs to share their IP and
  data with each other, and it has an adverse-selection problem with no fix
  (STRATEGY.md §3) — why would an org contribute knowledge that might help
  a competitor? Removing the contribution mechanism entirely dissolves that
  problem rather than mitigating it; there is no contribution decision left
  for any org to face adverse selection about (STRATEGY.md §14).

  What replaced it is closer to a vendor-maintained Stack Overflow or wiki
  than to anything shared between customers: a single corpus the *operator*
  authors and curates via `hub/manage.py commons-seed`, the only thing that
  ever writes a `commons_source == "seed"` row. No customer-facing tool can
  write to it, and no customer's own trace is ever in it — enforced not
  just by removing the sharing tool but by `commons_overlap` and
  `commons_search` both filtering explicitly on `commons_source == "seed"`
  in their SQL, a guarantee that holds even against a hypothetical future
  bug
  (`hub/tests/test_commons.py::test_a_shared_row_that_is_not_seed_sourced_is_still_invisible`).
  Consulting it stays optional per org (`commons_access`, a plan setting)
  and removable per deployment (`HUB_COMMONS_ENABLED=false`) exactly as
  before.

  `vote_trace` keeps its cross-org reach, narrowed to the same boundary:
  any org may vote on a Knowledge Base entry (`commons_source == "seed"`),
  never on another org's own private trace.

  This is the on-prem, self-learning fleet everything else in this repo
  already builds, plus an optional Knowledge Base layer next to it — not a
  peer-to-peer sharing network between customers.

### Removed
- **`share_trace` / `unshare_trace` MCP tools**, and the `commontrace
  commons contribute` / `commons unshare` CLI subcommands built on them.
  There is nothing for a customer to opt a trace into any more.
- **The "earn query allowance by contributing" mechanic**
  (`plans.QUERY_CREDIT_PER_HIT`, `entitlements()["commons_queries"]["earned"]`,
  `entitlements()["delivered_hits"]`). A plan's Knowledge Base query
  allowance is now a flat grant, because there is nothing a customer
  contributes to earn credit for.
- **`hub/manage.py commons-value` and `commons-stats`** — the per-org
  contribution ledger and the "how many distinct orgs contribute" adoption
  metric. Replaced by `hub/manage.py kb-stats`, a content-quality report
  for the operator (entry count, hits delivered, which entries have never
  matched anything) rather than a network-effect measurement, because
  there is no network effect to measure in this model.

### Fixed

- **`fleet_outcomes` was superlinear in an org's corpus (exponent 1.12),
  three commits after being added.** It selected every matching trace's
  `outcome` JSONB and counted in Python -- tens of thousands of blobs
  crossing the wire and a Python dict per row, to produce six integers.
  That is exactly the failure mode STRATEGY.md §13.2 names as fatal for
  the unit economics, shipped by the change that made §13.2's own
  measurement possible. Counting now happens in one grouped SQL aggregate:
  **572 ms -> 95 ms at 64,000 traces, exponent 1.12 -> 0.62.**

  The scan remains proportional to the org's history and that is not
  deferred work -- a question about all of history cannot be answered
  without reading all of it. What was removed is the per-row transfer. The
  next step if it ever matters is a materialized rollup, deliberately not
  built (it trades correctness-by-construction for a cache that can go
  stale).

  `outcomes.Tally` splits counting from statistics so the SQL and Python
  paths share one implementation of the significance logic, and
  `hub/tests/test_fleet_outcomes.py:TestSqlAndPythonCountingAgree` pins
  that they produce identical reports on identical data -- including the
  two traps where `::boolean` in SQL and `isinstance` in Python would
  diverge (a stringified `"true"`, a bool misfiled in a numeric field). A
  divergence there would not raise; it would change a customer-facing
  number silently.

- **The benchmark's first run blamed the wrong thing, and the fixture was
  the reason.** It reported `search_traces` as linear-or-worse (0.88).
  Every synthetic row shared near-identical title text, so the probe query
  matched 64,000 of 64,000 rows; `EXPLAIN` showed a sequential scan feeding
  a top-N heapsort, correct behaviour for a query where `ORDER BY
  ts_rank(...)` must score every match and no index can serve the ordering.
  With realistic text diversity the same path measures 0.19. Both the
  selective and the matches-everything cases are now reported, because the
  worst case is real. `TestGeneratedCorpusIsSelective` keeps the artifact
  from returning.

- **`hub/DEPLOYMENT.md` §6's scaling claim rested on one data point.** The
  existing "~113 ms sequential scan -> ~9 ms index scan at 50k traces"
  shows the index works and says nothing about growth; it now points at
  the measured exponents and carries the two caveats above.

- **CI was red: two new test files crashed pytest collection with no
  numpy installed.** `tests/test_m2_empirical_challenger.py` and
  `tests/test_storage_remediations.py` did `import numpy as np` unconditionally
  at module scope, so the core-install and dev-extra CI jobs (which don't
  install the `attention` extra) failed to even collect tests -- not a test
  failure, a collection error that aborted the whole run in under a second.
  My own local verification missed this because this sandbox has numpy
  installed system-wide, so `pytest tests/` never actually exercised a
  numpy-free environment despite reporting "all passed". Reproduced the
  failure properly this time in a fresh venv with no numpy at all (matching
  CI exactly), applied the project's own established pattern
  (`tests/test_benchmark_reports.py`'s `HAS_NUMPY` guard) to the 11 of 37
  tests across both files that actually need it, and confirmed: 516
  passed / 18 skipped where it previously errored out at collection.
- **`load_schema`'s path-traversal guard missed backslash/colon separators.**
  `os.path.basename` only treats `/` as a separator on POSIX, so a name like
  `C:\trace.schema.json` passed the "is this a bare filename" check
  unchanged on Linux, then 404'd instead of raising the intended `ValueError`
  -- not exploitable (backslash doesn't escape a directory on POSIX), but the
  wrong exception type reaching callers, and what the new regression test
  existed to catch. `\` and `:` are now rejected explicitly rather than
  relying on the host OS's path rules.
- **`sync`'s Hub URL had no explicit scheme guard**, despite a commit message
  claiming one. httpx already refuses to open a `file://`/`ftp://`
  "connection" so nothing was exploitable, but that safety was incidental to
  the HTTP client, not a guarantee this module made -- and a bad scheme burned
  the full retry budget (3 attempts, exponential backoff) before surfacing as
  an opaque "unhandled errors in a TaskGroup". Now rejected immediately with
  a clear message; verified live (1.05s of pointless retries -> 0.1s).
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
- **`commons_search` / `commontrace commons ask` — the commons becomes a
  knowledge base you can query, not just a meter that scores you.** The
  positioning has always described a searchable, ranked corpus where "each
  distinct problem need only be solved once". The commons shipped exactly
  one query surface: `commons_overlap`, which thresholds and returns a
  coverage *percentage* measured at 10.9% recall. There was no way to ask
  it a question and get an answer.

  §12.7 had shown that ranking rather than thresholding recovers the
  answers, but it showed it with the per-org ranker, which reads query
  **text** — so it did not transfer to a commons whose whole privacy
  proposition is that text never leaves the fleet. The version that does
  transfer was never measured. It is now
  (`commons/eval/search_modes.py`, which reproduces the published 10.9% /
  0% baseline exactly, validating the harness):

  | | Recall@1 | @5 | @10 | Text sent? |
  |---|---|---|---|---|
  | Threshold (ships) | 10.9% | — | — | No |
  | **Signature ranking** | **89.1%** | **95.7%** | **100%** | **No** |
  | Text ranking (§12.7) | 84.8% | 95.7% | 95.7% | Yes |

  **Ranking MinHash signatures beats ranking text at rank 1, with the
  privacy guarantee fully intact.** The 10.9% was never the matcher or the
  representation — it was purely the cutoff. Verified end to end against a
  live Hub seeded with the shipped 46-record corpus: "customer charged
  twice for one order" returns *Payment webhook delivered more than once*
  at rank 1 with its solution, at similarity 0.125 — which
  `commons report` correctly scores as **uncovered**, because a quoted
  number must not over-claim.

  Ships as a **separate** tool, deliberately. Ranked results are candidates
  to judge and are never coverage: absent failures return a non-empty list
  100% of the time and the score distributions overlap. `commons_overlap`'s
  threshold, its 0% false-positive property, and every number it emits are
  untouched. `commons_search` also does not credit `commons_hits` — that
  metric is the basis for earned allowance and contributor value and means
  "covered a real failure" at the conservative threshold; crediting
  candidates would make the one number that cannot be self-dealt trivially
  inflatable. It is metered as a commons query, absent when
  `HUB_COMMONS_ENABLED=false`, and scoped exactly like `commons_overlap`
  (own traces excluded, quarantined excluded, unshared never visible).

  STRATEGY.md gains §11.4a: §11.4's "binding constraint" (recall above
  ~60%, called a research task requiring embeddings and therefore a
  privacy retraction) is **met for lookup at 89.1% with no privacy cost**.
  It remains unmet for the coverage percentage, which is unchanged.
  commons/eval/RESULTS.md's "the honest path forward" is narrowed
  accordingly — embeddings are now an improvement to one number, not a
  precondition for the commons being useful.
- **Agents under management: the expansion meter, and the per-agent pricing
  it makes enforceable.** STRATEGY.md §12.6 concludes the variable to run
  this business on is "agents under management, not logos", and §13.1
  asserted it was already measurable. It was not. The Hub metered traces
  stored and commons queries and had no concept of an agent at all:
  `Trace.agent_type` is a CATEGORY (`support`, `sales`), so a fleet of 25
  support agents shared one value, `Plan` had no agent field, and a grep
  for `agent_id`/`max_agents` across the repository returned nothing. The
  per-agent tiers being sold (5 / 25 / unlimited) were unenforceable, and
  the metric the strategy names as decisive could not be computed — the
  same class of defect as the `--occasion-id` gap §13 found by checking a
  confident claim.

  Adds `Trace.agent_id` (protocol schema, CLI `capture --agent-id`, MCP
  `contribute_trace`, and the `sync` payload), `Plan.max_agents`,
  `crud.agents_under_management`, and per-org reporting in `manage usage`.
  Three design properties are load-bearing and are tested as such:

  - **Active in a trailing 30-day window, not distinct all-time.** An
    all-time count can only rise, so it could never show churn and would
    bill forever for a decommissioned agent. Retiring an agent frees its
    slot, exactly as purging frees storage allowance.
  - **The cap blocks expansion, never operation.** Enforcement runs only
    against a *new* agent; an org at its limit keeps serving every agent it
    already runs, and a plan downgrade below current fleet size does not
    break that fleet. Refusing the wrong write here would turn a commercial
    limit into a production outage, which is the one failure mode this
    feature could plausibly have caused.
  - **A floor, not a total, when `agent_id` is absent.** Legacy clients that
    send none are never rejected, but their traces collapse into a single
    `unattributed` agent; `manage usage` marks those orgs with a trailing
    `+` instead of quoting the figure as exact.

  STRATEGY.md §13.1 carries an inline correction recording that its claim
  was false, following the same convention as §12.7.
- **`maxLength` in `commontrace/validate.py`.** Required before the trace
  schema could bound `agent_id` to the Hub's column width: the validator
  enforces a deliberate subset and *raises* on an unknown keyword rather
  than ignoring it, so an unimplemented constraint cannot ship as a silent
  no-op. Its own test caught this. Over-long values now fail at `capture`
  time rather than surviving on disk and being rejected later at `sync`.
- **`commontrace taxonomy` / `commontrace impact` / `commontrace pilot` —
  the 30-day pilot's three leave-behinds, as real commands rather than a
  slide.** `taxonomy` groups recurring traces into a structured map of
  failure patterns (reusing `distill`'s clustering, but read-only and
  showing coverage status rather than writing candidate lessons). `impact`
  is the Impact Dashboard: errors avoided and lessons reused, counted
  directly from the same retrieval evidence `commontrace reliability`
  reads (not modeled), plus a dollar estimate that is only ever computed
  from a `--cost-per-1k-tokens`/`--value-per-error-avoided` rate the
  caller supplies explicitly — matching `hub/plans.py`'s "no currency
  appears anywhere in this repository, and that is deliberate"; omit both
  flags and it reports the measured counts with no dollar figure at all.
  `pilot` bundles both plus the baseline-vs-current resolution rate
  (`bench --pilot`) into one report ending in a yes/no gate, and the gate
  is conservative by construction: a randomized-holdout result
  (`commontrace experiment`) always outranks a correlational one, and
  correlational data alone tops out at "LIKELY -- not yet causal," never
  an outright yes. All three support `--json` and `--html` (a
  self-contained styled report written to `memory/benchmark_reports/`,
  the same convention `bench --pilot --html` already uses).
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
  into `memory/traces/`, so a fleet can start from its historical traces
  with no infrastructure replacement.
  Field-name mapping is configurable (a real export's column names
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
