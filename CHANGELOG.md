# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Immutable releases: what the fleet was running, as one named thing.**
  This product had two of the three identities a deployable change needs --
  a lesson (a mutable slug) and a revision (content identity for one
  lesson's text) -- and was missing the third. Nothing answered "what was
  the fleet running on Monday", which is the question rollback, attribution
  and atomic promotion all turn out to be: `status: active` is a property of
  each file NOW, carrying no memory of when the set changed or what from,
  and approving six lessons one at a time means the fleet runs five
  intermediate combinations nobody chose and nobody measured.

  `commontrace/release.py` adds a content-addressed, append-only snapshot of
  exactly which (lesson, revision) pairs were active together, what it
  replaced, and who cut it. It stores revisions rather than text, so it can
  never disagree with the lessons themselves. `commontrace release
  cut|list|show|diff|rollback` drives it.

  Three properties do the work. **Stale-base rejection**: cutting from a
  release the store has moved past is refused, because two curators each
  approving a lesson and cutting from what they saw loses a deployment
  decision, not a text edit. **A rewritten lesson is its own diff category**,
  not a remove plus an add -- "we changed what this rule says" and "we
  swapped one rule for another" are different deployments. And **a rollback
  refuses to restore a lesson whose text has changed since**: the target
  release pinned a revision the store no longer holds, so flipping a status
  would put back a different rule under the same name; `--allow-partial`
  proceeds once the operator has seen which ones. Rolling back appends
  rather than rewinds, because returning to an earlier state is itself a
  deployment and is the single fact everyone asks about afterwards.

  Deliberately does NOT gate retrieval yet: a release records the active set
  rather than deciding it, so a store that never cuts one behaves exactly as
  it does today. Making retrieval resolve through a release (so a fleet can
  run an older set without editing files, and canary/ring targeting has
  something to target) changes what every agent reads and belongs behind its
  own decision.

- **Pre-registration, and a raw export the customer can re-run the
  arithmetic from.** `experiment.plan` already worked out what it takes to
  answer the question before a run starts; nothing recorded that plan, and a
  plan nobody wrote down is a recollection formed after the result is known,
  by the party the result benefits. `commontrace/prereg.py` stores the
  commitments -- primary outcome, minimum practical effect, holdout rate,
  planned size, stopping rule, salt -- fingerprints them, and then DIFFS the
  run against them afterwards, which is the part that makes it more than a
  comment: a moved endpoint, a changed detectable effect, a different
  randomization, a fixed-n design stopped early, or a registration written
  after the data started arriving are each reported as a named deviation. An
  unregistered run says so rather than passing silently.

  `commontrace/raw_export.py` exports every arm decision -- including the
  assigned-but-never-reported ones, because those ARE the attrition question
  -- as CSV with a digest taken over canonical sorted rows, so it identifies
  the data set rather than the byte order a database happened to return. The
  digest and the pre-registration fingerprint are now part of the signed
  ledger's payload, which is what makes them anchored rather than merely
  available: an issuer cannot hand over a validly signed invoice alongside a
  data export that has nothing to do with it, because the signature commits
  to both.

  Wired through the Hub, not left as library code: `hub.manage
  start-experiment <org> [rate] [outcome] [notes]` registers the design at
  the moment the salt is minted (a new salt is a new experiment and does not
  inherit the last one's credibility), `causal_effects` and `value_delivered`
  carry the registration check and the evidence digest, and `hub.manage
  export-assignments <org> [file]` writes the rows out. `crud.holdout_
  assignments` is now the single definition of that query, because a second
  copy is a second chance for the invoice and the rows justifying it to
  describe different data.

- **A verdict read from a running experiment now survives having been
  watched.** Every surface here reads a LIVE holdout -- `experiment_status`,
  the console Proof page, `causal_effects` on every call, `working_set`
  promoting a trace the moment it clears significance -- which is repeated
  significance testing on accumulating data, the oldest way to manufacture a
  result. Measured on this estimator, in a world where the memory does
  nothing at all, the fixed 5% threshold declared HELPS or HURTS in **28% of
  runs**. That verdict promotes a memory into every later retrieval and
  feeds an invoice.

  `experiment.anytime_confidence_interval` is a Robbins-style normal-mixture
  confidence sequence: valid at every sample size simultaneously, so there
  is no stopping rule to violate and "peeked until it looked good" is not a
  way in. `analyze(sequential=True)` requires a result to clear both it and
  the existing multiplicity correction, and `hub/crud.py:causal_effects`
  passes it, because that is the surface being peeked. An alpha-spending
  schedule (`experiment.alpha_spent`, O'Brien-Fleming and Pocock) is also
  implemented and documented as insufficient on its own here -- it assumes
  the experiment stops at a planned size, and measured, it left 12.7%.

  The cost is stated rather than hidden, and was measured both ways: false
  positives 28% → 1.3% (nominal 5%), power within 1,000 occasions 95% → 69%
  at a +10pp effect and unchanged at 100% for +25pp and above. The sequence
  is tuned to where a memory worth promoting concludes rather than to where
  the design would exhaust itself -- without that, 60 occasions per arm
  showing a 50-point effect read as "cannot say", which is a miscalibrated
  instrument rather than a careful one. The default stays non-sequential for
  a one-shot analysis of a finished run, where the penalty would buy nothing.

- **The value aggregate no longer double-attributes occasions, no longer
  sums interval endpoints, and reports what its own selection is worth.**
  Three separate defects sat in the arithmetic that turned per-memory
  effects into the figure an invoice is computed from, and each one made a
  number that looked like a measurement.

  *Double attribution.* Summing `effect x n_injected` across memories is a
  count of occasions only if no occasion was counted twice -- and nothing
  guaranteed that. One support contact matching three traces, all injected,
  resolving once, was three improved occasions in the total, then three
  times the money. On the Hub this is the NORMAL case, not an edge one:
  `holdout_assign` takes a list of traces for a single occasion.
  `value.OccasionOverlap` (built from the assignment log the validity audit
  already reads) answers whether the sum is a count at all, and when it is
  not there is no total, no money and no ledger -- the same rule this module
  already applied to a compromised experiment, one level up. The per-memory
  effects are untouched: the addition was unsound, not the estimates.

  *And an aggregate that survives it.* Refusing a total would be the answer
  for nearly every real fleet, so `value.policy_effect` supplies the one
  that stays valid: occasions that received ANY memory against occasions
  that received none -- one row per occasion by construction, so
  double-counting cannot arise. It attributes nothing to an individual
  memory, which is the trade. Validated by simulation against a known
  ground truth: bias -0.003 on a true +0.15 lift, and 95% interval coverage
  of 95% over 40 runs.

  *The interval.* Summing the per-memory 95% ENDPOINTS is not a 95%
  interval for the sum under any assumption -- too wide for independent
  estimates, whose errors partly cancel, and undefined for dependent ones
  without the covariance. Now combined in quadrature, with the independence
  that requires being exactly what the overlap gate establishes.

  *The selection.* Counting only memories that cleared significance selects
  on the same data it then reports, biasing the total's magnitude away from
  zero. `occasions_improved_unselected` reports the same total without that
  selection, so the size of the winner's curse is a figure beside the
  billable one rather than a caveat nobody reads.

- **A store can require that the approver of a lesson is not its author.**
  Activating a lesson is the one action with fleet-wide blast radius -- an
  `active` lesson is injected into every later retrieval verbatim -- and
  the gate enforced everything about WHAT was being activated (no
  scaffolding, valid schema, no secrets or injection payloads) while
  leaving WHO entirely open: the same actor could draft a lesson and
  approve it a second later, so the independent second judgement the
  Validator role exists to supply was available rather than required,
  in exactly the case where it matters most -- an agent curating its own
  output unattended at machine speed. `commontrace/approval.py` makes that
  a policy the store states in `memory/approval-policy.yaml`:
  `mode: two-person` refuses an approver who appears among the lesson's
  recorded authors (read from the revision journal every content change
  already writes), and `require_human: true` refuses an `mcp:` actor's
  approval outright. Enforced on both approval paths -- the CLI's
  `lesson approve` and the MCP `approve_lesson` tool -- and `--force` does
  not override it, because that flag exists for an author judging a content
  warning a false positive, which is precisely the judgement this policy
  says that person may not make. With no policy file the behaviour is
  exactly as before. A malformed policy raises rather than falling back to
  the permissive default, since a security setting that silently disables
  itself is the one failure mode it must not have.

- **API keys carry scopes, so a credential minted for one job cannot do
  every job.** There was one identity per org and one privilege level: hold
  a key and you could search the corpus, contribute to it, delete a trace
  outright, and schedule the organization's own deletion. Least privilege
  is not a posture you can adopt with a credential that has no notion of
  less. `hub/scopes.py` defines three -- `read`, `write`, `admin` -- mapped
  to the three jobs a credential actually holds in a fleet (a dashboard
  reads; a production agent reads and writes; only an operator's key needs
  the destructive tools). They deliberately do NOT imply each other, which
  is what makes "can this key escalate?" answerable by reading one row
  instead of simulating a hierarchy. Every MCP tool declares its scope at
  the registration site via `scoped_tool`, and a test asserts no tool can
  be registered without that decision -- the failure mode designed out is a
  tool silently inheriting "any authenticated key may call this", which is
  what the whole surface did before. A denial returns
  `{"error": "forbidden", "required_scope": …, "granted_scopes": …}` rather
  than `unauthorized`, because the credential is valid and inviting a
  client to re-authenticate would turn a configuration error into an
  infinite retry loop. `issue-key <org_id> [days] [scopes]` takes the grant;
  omitted, it grants all three, so every existing key and the documented
  onboarding one-liner behave exactly as before (the column's server
  default backfills existing rows the same way).

- **The Hub now refuses to start when row-level security is installed but
  cannot bite, and the shipped stack serves as a role that cannot bypass
  it.** Postgres skips every RLS policy for a superuser or a `BYPASSRLS`
  role silently -- no error, no log line -- so "installed but inert" is a
  worse state than "not installed": a guarantee an operator believes in and
  does not have. This repo shipped exactly that, because the Postgres image
  makes `POSTGRES_USER` the cluster superuser and `docker-compose.yml`
  pointed `HUB_DATABASE_URL` at it, and the only response was one warning
  line at startup. Two halves now close it. `hub/postgres-init/
  10-runtime-role.sql` creates `commontrace_app` -- `NOSUPERUSER`,
  `NOBYPASSRLS`, owning nothing, holding DML grants only, with
  `ALTER DEFAULT PRIVILEGES` so every table a future migration adds is
  covered automatically -- and the compose `hub` service serves as it while
  `migrate` keeps the owner. `hub/db.py:check_row_level_security` (renamed
  from `warn_if_rls_is_inert`, because it can now refuse) raises unless
  `HUB_ALLOW_RLS_BYPASS=true` acknowledges the unsafe shape, with
  `HUB_REQUIRE_RLS=true` as the stronger opt-in form that also rejects
  policies being absent entirely. An unreachable database at boot still only
  warns: "cannot determine" is not "determined to be unsafe", and a
  diagnostic that crash-loops a process on a transient blip is worse than
  what it diagnoses. `hub/abuse.py`'s Postgres rate limiter now checks
  whether its table exists before issuing DDL, because Postgres refuses
  `CREATE TABLE IF NOT EXISTS` to a role without schema `CREATE` *even when
  the table already exists* -- without that, the database-backed limiter and
  the non-bypassing role would have been mutually exclusive.

- **Content-safety screening for lesson/trace text, closing the OWASP
  ASI06 (Memory & Context Poisoning) gap: nothing previously inspected
  what a captured trace or an activated lesson actually said before
  storing or injecting it verbatim.** New `commontrace/memory_guard.py`
  scans free text for three independent things: HIGH-confidence secrets
  (AWS/GitHub/Slack/Stripe/Google/Anthropic tokens, PEM private key
  blocks, JWTs -- structured shapes vanishingly unlikely to occur by
  chance), prompt-injection patterns (instruction-override phrasing,
  forged system-role blocks, jailbreak personas, hidden zero-width/bidi-
  override Unicode), and PII (email, phone, SSN-shaped numbers, Luhn-
  validated card numbers) -- the last of which never blocks anything on
  its own, only secrets and injection findings do. Wired into two gates
  that already existed: `hub/abuse.py:suspicion_reason` now also quarantines
  a `contribute_trace`/`amend_trace` submission that trips a blocking
  finding (excluded from `search_traces`, same as its existing spam
  heuristic); `commontrace lesson approve` and the MCP `approve_lesson`
  tool now refuse to activate a lesson that trips one -- an active lesson
  is injected into every later retrieval verbatim, so this is the one gate
  between drafted text and a live agent decision. The CLI path accepts
  `--force` for a human to override a false positive (e.g. a lesson that
  legitimately documents an example credential pattern); the MCP tool does
  not, since an agent approving its own draft has no interactive human to
  confirm one. See SECURITY.md's scope section.

- **Issuer signatures for the value ledger, so a hash chain nobody can
  forge a replacement for is now also a hash chain nobody can fabricate in
  the first place.** `verify_ledger` proves a ledger is internally
  consistent -- each entry follows from the one before it back to a fixed
  genesis -- but that genesis and the hashing algorithm are both
  deliberately public (the whole point is that a customer can reimplement
  the check), so anyone with write access to wherever a ledger is stored
  could regenerate an entire replacement chain from different figures and
  it would verify exactly as cleanly as the real one. `commontrace/value.py`
  adds `sign_ledger`/`verify_ledger_signature`: an HMAC-SHA256 over the
  chain's root, bound to the org and the issuance timestamp so a signature
  cannot be replayed onto a different org's ledger or re-presented later as
  fresher than it is. `hub/crud.py`'s `value_delivered` signs every
  response when the new `HUB_LEDGER_SIGNING_KEY` is configured, returning
  `signature`/`issued_at`/`signature_algorithm`; left unset, `signature` is
  `null` and `signature_reason` says explicitly that this deployment has
  not opted into issuer authentication, rather than letting an unsigned
  ledger silently look more audited than it is. See hub/DEPLOYMENT.md
  "Signing the value ledger".

- **A survival-analysis censoring check, so a lesson that works faster no
  longer looks like it is losing data.** `check_differential_attrition`
  compared terminal outcome-recording rates with no notion of time, and a
  lesson that helps concludes its occasions SOONER — so mid-run, the
  treated arm always has more outcomes on the books purely because it got
  there first. The check read that head start as attrition and returned
  `INVALIDATES`: effect unquotable, more data won't fix it. Measured on a
  fleet where treated reports in 5 minutes, control in 90, and nothing is
  ever lost: old reading `INVALIDATES` (96.7% vs 50%), new reading `OK`.
  `commontrace/survival.py` adds Kaplan-Meier and the log-rank test
  (stdlib only, same reasoning as this package's own two-proportion test
  and normal CDF), and an occasion now counts toward attrition only once
  it is older than the point by which the SLOWER arm's own outcomes had
  mostly arrived — the horizon is per-arm, not pooled, so a fast arm can't
  set the clock the slow arm gets judged against. The speed difference
  itself is reported by a new `censoring_hazard` check that can reach
  `WEAKENS` ("re-read later") and structurally cannot reach `INVALIDATES`,
  because timing is not bias.

- **A rate card and a hash-chained audit ledger for `value_delivered`.**
  A flat per-occasion rate prices a password reset and an averted SLA
  breach identically, which is the first thing a finance function
  rejects. `commontrace/value.py`'s new `RateCard` states the mix
  explicitly — named tiers, each a share of occasions and a cost per
  occasion — and refuses a card whose shares don't sum to one occasion.
  Every tier is echoed back on the wire as an INPUT, never a finding: none
  of it is measured, only `occasions_improved` is. When a rate is agreed
  and the run is readable, the response also carries a ledger — one line
  per counted memory, each entry's SHA-256 covering the previous entry's
  hash — so editing a figure, dropping the memory that `HURTS` before
  invoicing, or reordering to bury it all break the chain.
  `value.verify_ledger` recomputes it from the printed fields in a fixed
  order, so a customer's own auditor can reimplement it independently
  rather than trusting this codebase. The ledger inherits every refusal
  the number already had — no rate, an unaudited run, or a `COMPROMISED`
  experiment all yield an empty ledger, because a verifiable chain over a
  biased sample would make an unsupportable figure look audited, which is
  worse than no ledger at all.

- **A trivial-prompt gate, so an acknowledgement never pays for a
  retrieval.** A meaningful share of an agent's turns — "ok", "thanks",
  "lgtm", "go ahead" — carry no content to match a lesson against; ranking
  the corpus against them returns noise, and injecting that noise on an
  occasion it had nothing to do with is exactly the marginal-eligibility
  contamination `integrity.check_marginal_eligibility` exists to catch.
  `commontrace/cache_gate.py` (adapted from Nous Hermes Agent's
  `TRIVIAL_PROMPT_RE`) skips retrieval before the corpus is even read.
  Anchored at both ends so it never swallows a real query sharing a first
  word with an acknowledgement ("no results come back..." is not "no"),
  and — the property that makes it safe rather than merely fast — it runs
  BEFORE randomization: a skipped turn reads no store and is assigned no
  arm, so it never becomes an occasion at all. The same filter applied
  after assignment would drop occasions once their arm was known, which is
  the one thing a holdout cannot survive.

- **`working_set` — memory that costs its tokens once per session instead
  of once per query, and earns its contents.** Retrieval is not free:
  measured on a live Hub, one `search_traces` page costs ~231 tokens and
  is paid again on every call. The simplest design in the field, Nous
  Research's Hermes Agent (MIT), avoids that entirely by reading two small
  files into the system prompt once at session start and never changing
  them mid-session, deliberately, so the provider's prefix cache is never
  invalidated — roughly 1,300 tokens for a whole session and zero marginal
  cost per query.

  What Hermes cannot do is decide what belongs in those tokens; it asks
  the agent to curate its own notes. Mem0, Zep and Letta fill their
  equivalents by automatic extraction and grade themselves on recall
  benchmarks (LoCoMo, LongMemEval) — "did you remember the fact", never
  "did remembering it make the work go better". This Hub already answers
  the second question per trace, so `working_set` selects by MEASURED
  CAUSAL EFFECT: a trace is pinned only once the fleet's own randomized
  holdout has established it as HELPS, ranked by occasions actually
  improved. The experiment stops being only a report and becomes the
  promotion mechanism.

  That rule also resolves what would otherwise be a methodological hole. A
  trace pinned into every session is injected on every occasion, which
  would destroy the control arm still measuring it — so restricting the
  block to established effects means nothing under test is ever pinned. A
  trace is either being randomized or it has graduated, never both. A
  COMPROMISED experiment yields no block at all, for the same reason it
  yields no value figure, and a fleet with no established effects yet gets
  an empty block that says so rather than a most-retrieved list that would
  look identical while carrying no evidence.

  Measured live, end to end: a trace promoted after its effect was
  established at +51% over 63 occasions produced a 112-token block against
  231 tokens per search — **41x cheaper at 20 lookups per session, 103x at
  50** — while remaining strictly additive, since `search_traces` still
  reaches the entire corpus including everything still under test.

- **Graduation into `working_set` now expires, so a pinned lesson cannot
  outlive the evidence for it.** The promotion rule above creates its own
  blind spot: a pinned trace is injected on every occasion, so it is never
  withheld, so it stops accumulating the withheld arm its effect was
  computed from. Graduation *freezes* the measurement. Left alone, "has a
  measured causal effect" quietly decays into "had one once, against a
  world that has since moved on" — and the upstream fix, API change or
  dependency bump that made the lesson obsolete are all invisible to it.

  Every competing system addresses the same staleness with a **proxy for**
  usefulness rather than a measurement of it. Mem0 scales retrieval rank by
  an Ebbinghaus-style recency/access curve (~0.3x–1.5x, a soft rerank, not
  a delete); the 2026 survey literature converges on "differential
  exponential decay keyed to relevance, access frequency and temporal
  pattern". All of them can only see whether a memory was recently *read*.
  A lesson nobody happened to retrieve decays; an obsolete lesson everyone
  keeps retrieving does not.

  This Hub measures the thing itself, so it does not have to guess — and it
  deliberately does not claim what it cannot see. `causal_effects` now
  dates every estimate (`last_measured_at`, from the last resolved
  observation the estimate was actually computed from), and `working_set`
  drops any entry whose evidence is older than a configurable horizon
  (`evidence_horizon_days`, default 180). The reason string is explicit
  that this is **not** a finding that the lesson stopped working: it is
  that nobody has checked. Leaving the block is precisely what returns the
  trace to the randomizer, which is the only mechanism that can produce a
  fresh answer, so the horizon acts as the working set's *renewal period*
  rather than a punishment — the trace is promoted again as soon as the
  experiment re-establishes it.

  Each entry also carries `evidence_age_days`, and both new fields are
  structured metadata only: they are deliberately kept **out** of the
  pinned `block` text, because an age changes daily and a block whose text
  changes daily invalidates the prefix cache the whole feature exists to
  preserve. Pinned by a test.

### Fixed

- **A pinned `working_set` trace could be drawn into its own control arm,
  silently biasing the effect it was promoted for.** `working_set`'s
  contract says a trace is "either being randomized or it has graduated,
  never both", and nothing enforced it. The block goes into the system
  prompt for a whole session; `search_traces` (via `holdout_for_results`)
  then assigns arms to every result it returns, promoted traces included.
  When one drew the WITHHELD arm, the occasion was recorded as a control
  while the trace was still sitting in the agent's prompt — a treated
  occasion counted as untreated. `holdout_assign`'s own note already
  described the consequence precisely: it "does not fail loudly — it biases
  the measured effect toward zero". Nothing in `integrity.audit` could see
  it either, because the arms remain balanced and deterministic; only the
  *content of the prompt* was wrong, and the Hub never saw that.

  Left alone this is self-defeating: promotion contaminates the estimate
  that justified the promotion, eroding it until the trace is demoted
  again, and the fleet's headline `value_delivered` figure is biased down
  with it.

  Only the caller knows what it actually pasted, so `search_traces` and
  `holdout_assign` now take `pinned` — the `entries[].trace_id` a
  `working_set` block handed back. Those ids are excluded from the
  randomization entirely and reported as `inject` with **no observation
  recorded**, so they contribute to neither arm. They are dropped *before*
  near-duplicate clustering, not after: `_cluster_representatives` can
  elect a pinned trace as a cluster's representative, and the whole cluster
  would otherwise inherit an arm from a trace that has none. Strictly
  additive — omitting `pinned` behaves exactly as before.

- **`working_set` kept pinning a promoted trace's text after that trace was
  corrected or deleted.** The block is pasted into a system prompt once and
  never re-fetched mid-session, so unlike `search_traces` nothing ever
  re-reads it to notice staleness — which made this the one surface where
  the bi-temporal supersession gap below would never have been spotted.
  Amending a promoted trace (`amend_trace` writes a NEW row) left its
  *pre-correction* wording pinned indefinitely: the effect was real, but
  the text backing it had been superseded. Purging one
  (`hub/manage.py:purge_trace`) was worse — the block went on pinning a
  `"(deleted trace)"` placeholder title with an empty solution body, so the
  prompt carried a lesson with nothing in it to read.

  Both now drop out of the block entirely, following the same "degrades
  honestly rather than inventing a block" rule the function already applied
  to a compromised experiment. There is deliberately no "resolve forward to
  the amended version" here, unlike `search_traces`/`commons_visible`: the
  effect was measured against the specific text shown on each occasion, so
  inheriting it onto rewritten wording would be presenting evidence for
  content nobody ever tested.

- **A relevance tie returned a fleet's own re-tellings of a lesson ahead
  of the lesson itself, and the near-duplicate clustering could not hold
  a stable randomization unit because of it.** Exact `ts_rank` ties are
  the signature of near-duplicate text, and a fleet generates those
  constantly: it resolves an occasion using a lesson, then contributes a
  trace describing what happened in the same words. `search_traces` broke
  those ties newest-first, so as occasions accumulated the original fell
  off page one entirely and every result was a re-telling. That is the
  worse of the two results to hand an agent, and it also left the
  clustering with no fixed member to anchor to: `holdout_for_results`
  only ever sees ONE PAGE, so a representative chosen from page members
  drifted as the page composition shifted, re-creating the very
  fragmentation the clustering exists to remove. Measured live over 20
  occasions of one lesson: **8** distinct randomization units, with only
  4 injections on the best-measured one. Ties now break oldest-first
  (recency still orders the no-query browse path, which is what that path
  is for), and a cluster's representative is its oldest member rather
  than `distill.representative`'s page-dependent medoid. Same scenario,
  same seed, after the fix: **1** unit, **15** injections on it -- ~3.75x
  the evidence accumulated per occasion, and 21x versus the unclustered
  behavior the deployment audit originally measured.

- **The Hub's own randomized holdout fragmented a fleet's statistical
  power across near-duplicate traces.** Every occasion a fleet resolved
  and then contributed a trace of -- the exact pattern `commontrace
  capture` encourages locally -- became a new, independent randomization
  unit in `holdout_assign`, so instead of one lesson's injections
  accumulating on one id, they split across dozens of near-identical
  traces that individually never cleared the power threshold. A real,
  deployed audit measured this at ~19x: 56-59 tracked "memories" per
  vertical against 3 lessons actually seeded, with two of four verticals
  showing $0.00 in `value_delivered` despite real, large effects (+62%,
  +56%) that simply never accumulated enough injections on one id to
  reach significance. `hub/crud.py:holdout_for_results` now clusters
  near-duplicate search results with `commontrace.distill` (the same
  clustering the local tier's own `commontrace distill` uses) before
  assignment, preferring a cluster member with no recorded failed
  outcome as the representative; the wire response is unchanged; a
  re-run of the same audit against the fix measured the tracked-memory
  count dropping from 21 to 8 for one 20-occasion vertical.
- **A trace's own recorded failure could outrank a working solution for
  the same query.** `search_traces` ranked purely on text relevance, so
  an agent's unresolved, escalated occasion log and a hand-written,
  working lesson describing the same failure ranked on equal footing --
  text relevance cannot tell them apart, since both describe the same
  failure in the same words. A live spot-check found the canonical
  lesson missing from the top 5 results entirely in 2 of 3 real queries,
  buried behind its own low-quality duplicates. Traces with
  `outcome.resolved: false` now sort after every other result at the
  same relevance -- a ranking floor, not a filter, so a failed attempt
  stays findable, just never ahead of a better-standing result. Re-run
  against the fix: the same previously-buried lesson now ranks #1.
- **`search_traces(brief=True)` shipped ~14 always-present metadata
  fields even at their empty/default value**, so the docstring's own
  recommended "browse many with brief, then `get_trace` the one you
  want" pattern measured *worse* than a single non-brief call, not
  better, on a real corpus. Brief mode now omits an operational field
  (`agent_id`, `extensions`, `votes`, `outcome`, ten more) when it holds
  its default value; a field that actually holds something still ships.
  Full mode is unchanged. Measured: brief-mode payload savings on a
  5-result page went from 16% to 37%.
- **`value_delivered` returning $0.00 read as "this doesn't work" rather
  than "not enough data yet"** when every memory was UNDERPOWERED --
  `reason` stayed empty in exactly that case, with no top-level signal
  distinguishing a correct measurement from a null result. It now names
  the strongest (unestablished) trend and its current injection count
  directly, e.g. "the strongest trend so far is `X` at +62% on 11
  injection(s)."
- **`pytest`'s dev-extra range still permitted a known-vulnerable version,
  and fixing it broke `pytest-asyncio` collection.** `pyproject.toml`'s
  `[dev]` extra capped `pytest` at `<9.0`, whose newest release (8.4.2)
  carries PYSEC-2026-1845, fixed in 9.0.3. CI's own `pip-audit` job flags
  any version a range specifier *permits*, not just the one actually
  installed, so the cap was making that job fail regardless of what a
  contributor's environment happened to resolve. Raised the floor to
  `>=9.0.3` (not just the ceiling) so the vulnerable range is
  unreachable — which then surfaced a second, real incompatibility:
  `pytest-asyncio`'s old `<1.0` cap resolved 0.23.3, whose
  `pytest_collectstart` hook breaks under `pytest>=9`
  (`AttributeError: 'Package' object has no attribute 'obj'`). Raised its
  floor to `>=1.0` too, matching the unpinned `pytest-asyncio` CI's own
  `hub tests` job already resolves. The full suite (client + hub) is
  verified green on pytest 9.1.1 / pytest-asyncio 1.4.0, including the
  exact three-step CI job (`pytest tests/`, `ruff check .`,
  `pytest hub/tests/test_image_contents.py --noconftest`) that first
  caught the collection break.

- **A non-finite `rank` in a holdout log line crashed the entire read.**
  `json.loads` accepts the bare `Infinity`/`-Infinity`/`NaN` tokens by
  default, and `holdout_io._opt_int` converted with `int(value)`, which
  raises `OverflowError` on an infinite float — uncaught, since the append
  that used it sits outside the per-line corrupt-line guard. One such line
  took down `read_log` for the whole file, violating its own documented
  contract that "one torn write must not make the rest of an experiment
  unreadable." `_opt_int` now catches `OverflowError` alongside
  `TypeError`/`ValueError`, matching the non-finite guard `_opt_float`
  already had.

- **`COMMONTRACE_ALLOW_STORE_SCRIPTS=1` never actually did anything.**
  `find_reference_script` checked the packaged copy of a reference script
  first and returned on the first match — but every real reference script
  (`query.py`, `build_index.py`, `measure_performance.py`,
  `pilot_metrics.py`) ships inside the package, so the packaged candidate
  was always a hit and the store-root candidate was never reached, even
  with the opt-in set. A contributor editing a reference script in their
  own checkout got the stale packaged copy every time, silently. Now,
  when opted in, the store-root copy is checked first (and wins).

- **A malformed `tags` argument to the `capture`/`draft_lesson` MCP tools
  was silently dropped.** `_coerce_tags` returned `None` for anything that
  wasn't a string, list, or tuple, and both call sites treated that the
  same as "no tags were given" — the write succeeded, `ok` was `true`, and
  nothing in the response said the tags never landed. It now raises
  instead, so a malformed value surfaces as a clear tool error.

- **`draft_lesson` had no size guard on the text it writes.** The
  CLI write paths (`capture`, `lesson new`, ...) all refuse an oversized
  write via `_validators.check_text_size`, but `draft_lesson` — the
  primary way an agent puts free-text content into a lesson over MCP —
  called `lesson_io.write_lesson` directly with no size check at all. It
  now runs the same guard before writing.

- **A cache entry with a corrupt `terms` field never self-healed on disk.**
  `lesson_cache`'s write-decision (`_stamps_differ`) compares only
  `mtime_ns`/`size`/`fm`, deliberately not `terms` (a pure function of
  `fm`). But a `terms` field that was corrupt for some other reason (a
  hand-edited or format-drifted cache file) got correctly recomputed in
  memory on every call — and then never written back, since the stamps it
  compares hadn't moved. That lesson paid a full reparse on every single
  future call, forever, silently defeating the module's own "an edit
  costs one parse, not N" guarantee for that entry. The repair is now
  flagged explicitly and forces a cache rewrite.

- **`failure_import.read_failures`'s fallback size cap counted characters,
  not bytes.** If `os.path.getsize` failed while `open()` still succeeded,
  the fallback opened the file in text mode and capped `len(raw)` —
  decoded characters — letting up to ~4x `MAX_IMPORT_BYTES` of real data
  through on multi-byte UTF-8 content. The fallback now reads bytes
  directly and decodes after the cap is enforced.

- **`commontrace import`'s size cap and implicit-store warning had gaps.**
  `import_data.py` never received the total-file-size cap
  `failure_import.py` got (only the per-field CSV limit was duplicated);
  `import_cmd.py` now checks the file size up front. Separately,
  `warn_if_implicit_cwd_store` used `explicit is not None` where
  `resolve_root` uses a truthy check, so `--dest ""` suppressed the
  warning while still hitting the same cwd fallback as no `--dest` at all
  — now consistent. The warning is also wired into `commontrace import`
  and `commontrace distill`, which create a store the same way
  `capture`/`lesson new` do but had no warning at all.

- **Checkout could mint a second Stripe subscription on an already-subscribed
  org.** The Overview page hid the "Upgrade" button once an org had a live
  subscription, but the `billing_checkout` route itself never checked —
  a stale page, a browser back-button resubmit, or a direct POST reached
  `create_checkout_session` regardless. Checkout always creates a NEW
  subscription (an already-subscribed org is supposed to go through
  Stripe's Billing Portal instead); minting a second one on the same
  customer is silent double billing, not a cosmetic bug. Now refused at
  the same point every other invalid state in that handler already is.

- **Deleting an org with a live Stripe subscription never cancelled it.**
  Both `confirm_org_deletion` (self-service, reachable by any org's own
  API key) and `hub.manage purge_org` (operator CLI) deleted the org row
  — and with it, `stripe_subscription_id` — without telling Stripe.
  A deleted account with an uncancelled subscription keeps being charged
  every billing cycle with no CommonTrace account left to ever notice.
  Both paths now cancel a live subscription first; if that fails, nothing
  is deleted (a new `deletion_blocked` MCP error code on the self-service
  path, an error message and `False` on the CLI path) rather than
  deleting the account and stranding the subscription.

- **Self-serve billing could be partially configured into a paid-and-never-
  upgraded state.** `StripeSettings.checkout_configured` checked for a
  secret key and a price, but not `HUB_STRIPE_WEBHOOK_SECRET` — a
  deployment missing only that variable would show a working "Upgrade"
  button, take a customer's real payment via Stripe Checkout, and have no
  route left to ever learn it happened, since `Organization.plan` only
  ever updates from the webhook. The same gap existed on the Billing
  Portal route (`stripe.secret_key` checked, `webhook_secret` not),
  where an already-subscribed customer could cancel and keep their paid
  entitlement forever with nothing to notice the cancellation. Both
  routes now require the full, working round trip before ever redirecting
  to Stripe.

- **`retrieve()` (the local MCP tool) shipped a withheld lesson's full body
  over the wire.** The control arm of the tool's own randomized holdout is
  the half an agent is explicitly told never to act on — but every
  withheld lesson's complete instructional text was still being sent on
  every `retrieve()` call made while an experiment runs, which is exactly
  the traffic pattern of a customer rigorously proving this product's own
  causal claim. `commontrace query` (the CLI path to the same holdout) has
  never printed a withheld lesson's body; `retrieve()` was the one surface
  that disagreed with its own documented contract. Metadata (slug,
  description, tags, score) still ships — only the body is withheld along
  with the lesson itself.

- **`experiment_status` (the MCP tool) reintroduced a bug this file already
  recorded as fixed once.** The CLI's `commontrace experiment` scopes its
  report to the store's *current* randomization (salt) — changing the
  holdout rate rotates the salt on purpose, and pooling assignments from
  two randomizations lets one occasion sit in opposite arms, which
  `integrity.check_assignment_drift` correctly flags as COMPROMISED. The
  MCP tool added later for the same report never got that scoping: it read
  every assignment ever logged, so an operator who followed the documented
  workflow (`experiment --configure` to widen a holdout) would see the CLI
  report a clean, running experiment while an agent calling
  `experiment_status` on the identical store saw COMPROMISED — two
  surfaces disagreeing about the same store. The scoping logic is now a
  shared `experiment_cmd.scope_to_current_salt()` both callers use, so the
  two cannot drift apart again the same way. `tests/test_mcp_server.py`
  drives this over a real MCP call, not just the underlying function.

- **`amendment_chain()` walked a trace's amendment lineage with one
  database round trip per link.** `hub/crud.py`'s BFS issued one query per
  level of the chain, and nothing capped how deep a chain gets — repeated
  `amend_trace` calls on the same trace is this codebase's own documented
  curation pattern ("each attaching whatever became known since"). Reachable
  via the customer-facing `delete_trace` MCP tool (not just the
  operator-only `purge_trace`), so an org that built one very deep chain
  could make a self-service delete hold a pooled database connection open
  for thousands of sequential round trips. Replaced with a single query —
  two independently-recursive CTEs (one per link direction, unioned), since
  Postgres allows a recursive term to self-reference at most once and a
  single bidirectional recursive term is rejected outright
  (`InvalidRecursionError`, caught by running this against a real Postgres
  16 instance, not just a compiled-SQL read). `hub/tests/test_manage.py`
  asserts a 30-deep chain still costs at most two queries and returns the
  exact right id set.

- **A working, user-facing local flag had its value silently dropped at
  the Hub sync boundary.** `commontrace capture --profile <name>` has
  populated `Trace.profile` on disk since the schema was written, and the
  Hub already modeled the column, read it back on every `get_trace`/
  `search_traces`, and correctly carried it forward on every `amend_trace`
  call — but `contribute_trace`, the only place a NEW trace is ever
  created, had no parameter for it at all. A captured trace's `--profile`
  value therefore vanished the moment `commontrace sync --push-traces`
  sent it to a Hub, with no error anywhere. `contribute_trace` now accepts
  `profile`, validated the same way every other free-text field is
  (`reject_unstorable_text`, a length check against the column's actual
  `String(128)` width) and folded into the idempotency hash only when set,
  so every existing caller (including `submit_kb_entry`, which never
  passes it) keeps hashing exactly as before. `hub_client.py`'s trace-push
  path now forwards it. `extensions`/`watch_condition`/`review_after`
  — the other three fields `amend_trace` already carries forward
  unconditionally — stay at their contribute-time defaults: nothing
  anywhere populates them yet, unlike `profile`, and `extensions` being an
  open `additionalProperties: true` object needs its own validation pass
  (a NUL byte or lone surrogate nested inside a JSONB value fails at
  INSERT the same way a flat string column does) before it can safely
  accept arbitrary customer JSON. 7 new tests in
  `hub/tests/test_profile_input.py` and `tests/test_hub_client.py`.

- **A customer could never learn why an operator declined their Knowledge
  Base proposal.** `hub/console.py`'s "Note" column read
  `s.get('reviewer_note')`, a key `crud._submission_to_wire` never
  produces — the field is named `rejection_reason` there. The lookup
  always returned `None`, so the column rendered "—" unconditionally, no
  matter what an operator actually typed as the reason.

- **The admin overview's fleet-wide tiles silently undercounted past 200
  organizations.** `traces_by_org`/`quarantined_by_org`/`keys_by_org` in
  `hub/admin.py:_overview` are already unrestricted, fleet-wide `GROUP BY`
  aggregates — but `total_traces`/`total_quarantined`/`total_keys` were
  summed from `rows`, the per-org table capped at the first `_MAX_ROWS`
  (200) organizations by name. Past 200 orgs, the three top-line tiles an
  operator scans first would quietly stop matching reality, with no
  truncation notice anywhere near them (the one that exists sits below the
  per-org table). Fixed by summing the already-complete aggregate dicts
  directly. The admin Knowledge Base page had the identical defect on its
  "awaiting review"/"needs attention"/"retracted" tiles, taken from lists
  capped the same way; `kb_review_queue`'s four-bucket classification is
  now split into a shared, unbounded `_kb_review_queue_full` so a new
  `count_kb_review_queue` can report the true total without a second,
  potentially-drifting reimplementation of the same buckets. All four
  affected sections now carry a truncation notice when the rendered list
  is shorter than the true count.

- **The customer-facing memory-search page had no way to reach a result
  past the 50th.** `search_traces` has returned `offset`/`has_more`
  since the fix for this product's own core retrieval defect (its own
  docstring), but `hub/console.py`'s `/memory` route called it with no
  offset and never read `has_more` back — a corpus with more than 50
  matches showed exactly 50 rows with no indication more existed and no
  control to page further. Wired through to Newer/Older links.

- **Doc drift**: `hub/README.md`'s tool listing said "Twelve more are
  Hub-specific," omitting `value_delivered` — the tool implementing this
  product's actual pricing mechanism (`STRATEGY.md` §11.5) — from both the
  prose and the count. `README.md`'s local MCP tool table omitted
  `experiment_status`. `DATA_RETENTION.md` still described the Hub as
  exposing "the six MCP tools," a claim its own later section already
  contradicted (and the Hub's real surface has grown well past that).

- **`GET /metrics` had no cardinality bound on the HTTP method label.**
  `path` was already bucketed to the routes this app actually serves
  ("the classic way a metrics endpoint becomes the outage," per that
  code's own comment) — `method` was not, and an HTTP method is
  constrained only by RFC 7230's `token` grammar. An unauthenticated
  caller sending arbitrary distinct verbs at any path (including ones that
  404 and are rate-limited nowhere, since `ApiKeyAuthMiddleware` only
  meters the MCP path) could grow `Metrics._requests` — a plain,
  never-evicted, process-lifetime dict — without bound. Bucketed to the
  same fixed set of standard HTTP methods `path` already uses.

- **`kb_stats`'s "needs review" hint covered two of `kb-review`'s four
  buckets.** It was computed as
  `standings[disputed] + standings[stale]` — `standing_of()` has no
  "urgent" (security-flagged) or "never_hit" value at all, so an entry in
  either of those two buckets was invisible to this summary while
  `kb-review` itself listed it first, security flags included. Now reads
  `crud.count_kb_review_queue`, the same true, unbounded count the admin
  console's "needs attention" tile uses after its own fix above — one
  function, not two classifications that could silently disagree.

- **`approve-submission` printed the credit it was asked for, not the
  credit it actually wrote.** `review_kb_submission` clamps `--credit` to
  `[0, 2**63-1]` before storing it (so `-50` writes `0`), but the CLI's
  success message printed its own unclamped local variable — an
  operator-facing report that misdescribed what the database actually
  did. Now prints `result["credit_awarded"]`.

- **A destructive-operation confirmation prompt crashed on Ctrl-D.**
  `purge-trace`/`purge-org`'s interactive "type 'yes' to continue" prompt
  let an ordinary EOF (Ctrl-D) raise `EOFError` uncaught, reaching the
  operator as a raw traceback instead of the same clean "aborted" message
  every other way of saying no already gets — before anything destructive
  had happened.

- **`commontrace import --dry-run` could report a row as importable that
  a real run would reject.** Schema validation ran only in the
  non-dry-run branch, so a row that parsed fine but failed
  `trace.schema.json` (e.g. a negative `tokens_used`, which the schema
  floors at 0) was counted by `--dry-run` as one of the traces "would be
  created," while the real run on the identical file rejected it and
  exited non-zero — exactly the outcome a dry run exists to predict.
  Validation now runs before the `--dry-run` early-exit; reject samples
  are reported (and folded into the exit code) identically in both modes.

### Changed

- **`revision.py:same_treatment()`** had zero callers anywhere in the
  repository, including tests. Removed rather than left unreachable.
- **`HubConfig.api_key_header`** was defined but never read from the
  environment and never consulted by the auth middleware, which hardcodes
  `"Authorization"` everywhere — a config field that silently did nothing
  no matter what it was set to. Removed.

All of the above verified against a real Postgres 16 instance (not
SQLite/mocks): the full suite (2067 CLI + Hub tests) passes, plus 22 new
regression tests covering each fix, and a security review of the complete
diff found no newly-introduced vulnerability.

### Added

- **Shareable, read-only Proof links.** Until now the Proof page — the one
  place this product's central claim (a causal, honestly-caveated
  measurement of whether the memory changed outcomes) is actually shown —
  only ever rendered behind an authenticated console session. A signed-in
  user can now mint a time-limited (14 day), org-scoped share token from
  the Proof page; the resulting link needs no session and re-runs the same
  live queries the authenticated page does, so it can never go stale into
  something misleading. Kind-separated from session tokens in both
  directions, uniform 404s on an invalid/expired link, and rate-limited
  per-org rather than per-address.

- **Self-serve org signup (`HUB_SIGNUP_ENABLED`).** Every account before
  this was sales- or support-assisted by construction: the only way to get
  an org and a first API key was an operator running `hub.manage
  create-org`. A public, opt-in `/signup` route now lets a visitor create
  their own free-plan org with nobody involved — no email verification
  (this Hub has no outbound email integration), bounded by the free plan's
  own limits either way, with a tight per-address rate limit and a
  honeypot as the actual abuse controls.

- **Self-serve billing (`hub/billing.py`).** A signed-in customer can now
  upgrade to a paid plan via Stripe Checkout, and `Organization.plan`
  stays in sync with what Stripe actually charged via a signed webhook —
  no operator, no manual row edit. An already-subscribed org is routed to
  Stripe's Billing Portal instead of through Checkout again. No Stripe
  SDK: REST calls over `httpx` (already a dependency) and one documented
  HMAC check for webhook verification.

- **`python -m hub.manage value <org_id> [value_per_occasion]`.**
  `crud.value_delivered` (occasions improved, causally, priced only when a
  rate is supplied and never stored) was reachable from a customer's own
  console session and from an authenticated agent's MCP tool call, but an
  operator investigating one account had no CLI path to the same numbers
  without holding a customer's own API key. Also the first caller of
  `plans.billable_value()`/`VALUE_CAPTURE_SHARE` from anywhere reachable
  by an operator.

- **`search_traces(brief=True)`.** `context_text`/`solution_text` are each
  allowed up to 20,000 characters, and a search page can hold up to 200
  results — a full page can legitimately run to millions of characters,
  enough to blow a calling agent's own context budget while it is still
  deciding which result to use. `brief=True` (off by default) previews
  both fields instead, word-boundary-safe and always marked `"brief":
  true`; everything else on each result is untouched, matching the
  local tier's existing `list_lessons`/`get_lesson` "browse then get"
  split.

- **CI now proves self-serve signup and console sign-in over real HTTP.**
  The `compose-stack` job builds and runs the actual container image —
  the only CI job that catches what only shows up when "the pieces
  compose" — but never enabled or exercised either surface above; a
  route-registration mistake or an env var not reaching the container
  could have shipped invisibly. A new step drives the real HTTP surface
  end to end: signup issues a key, and that exact key signs in to the
  console.

- **`SECURITY.md`.** This Hub is multi-tenant, stores customer trace data,
  and holds an Argon2-hashed API key per organization — and had no
  vulnerability disclosure policy anywhere. Points reporters at GitHub
  Security Advisories (private by default) instead of a public issue,
  states in-scope/out-of-scope classes matched to this codebase's actual
  trust model, and says plainly that there is no dedicated contact, SLA,
  or bug bounty yet rather than implying one that doesn't exist.

- **`hub/requirements-lock.txt`: a real answer to the "known follow-up"
  `hub/requirements.txt` already named** ("this still isn't full
  transitive-dependency reproducibility... pinning every indirect
  dependency too"). Not theoretical: a clean install of
  `hub/requirements.txt` today resolves `mcp`'s own transitive
  `httpx2==2.12.0` and `starlette==1.6.0` — both legitimate, current
  releases from httpx/starlette's original authors, not a supply-chain
  issue, but versions that didn't exist when this Hub was last tested
  against `mcp`'s transport, and neither pinned anywhere. Generated as a
  pip constraints file against Python 3.12 (the Dockerfile's base image,
  the Hub's actual shipped runtime) — deliberately NOT applied to CI's
  3.10/3.11/3.12 matrix, since a clean install under 3.10 was verified to
  resolve a genuinely different transitive set (`async-timeout`/
  `exceptiongroup`/`tomli` backports, an older `rpds-py`), so a single
  lock file would be wrong, not merely redundant, for two of those three.
  Wired into the `Dockerfile` via `-c` (constraints, not requirements);
  the full `hub/tests/` suite (918 tests) passes against the exact
  constrained install, confirming it works rather than merely resolves.
  `hub/tests/test_requirements_lock.py` fails the build if a direct pin
  and the lock ever disagree.

- **`.github/dependabot.yml`.** Pairs with the lock file above — a lock
  nobody is prompted to revisit rots exactly the way its own header warns
  against. Covers `pip` (both `/` and `/hub`), `docker` (the Dockerfile's
  base image), and `github-actions` (the workflow's `actions/checkout`/
  `actions/setup-python`, pinned to full commit SHAs specifically so a
  compromised tag can't inject code into CI — Dependabot bumps the SHA and
  its version comment together, so that guarantee survives routine
  updates). Does not, and cannot, regenerate `requirements-lock.txt`
  itself — that needs a real interpreter to resolve against, per its own
  header — so a merged bump is the trigger to run those steps by hand.

- **The causal instrument and the commercial number were never connected —
  and the commercial one was the confounded one.** `STRATEGY.md` §11.5 names
  this product's pricing hypothesis (price against measured effect per fleet,
  not seats or trace volume) and says "the mechanism ships". The effect size
  shipped. Nothing turned it into a quantity a price could attach to, the Hub
  — the surface customers pay on — computed **no value at all**, and the one
  estimator that existed (`commontrace impact`) is correlational by its own
  admission in five places.

  `commontrace/value.py` computes, per memory whose causal effect the holdout
  has established, `effect × n_injected` — how many more occasions went well
  *because* that memory existed, carrying the confidence interval through. A
  count, in the fleet's own units. Available as `commontrace experiment
  --value-per-occasion`, the Hub's `value_delivered` MCP tool, and on the
  customer console's Proof page (`?per_occasion=25`).

  **Still no currency anywhere.** §11.5's argument for that is correct and
  unchanged: the caller supplies what one resolved occasion is worth to them,
  this supplies how many there were, nothing is stored.

  Three rules make it a measurement rather than a brochure:

  - **A COMPROMISED experiment produces no figure** — not a hedged one. A
    value report is exactly where a caveat gets separated from its number.
  - **An UNDERPOWERED memory contributes nothing.** Measured on a real run: a
    memory reporting +30% on 90 occasions, never established, would have added
    a phantom +27.
  - **Memories measured as HURTING are subtracted, not dropped.** A figure that
    sums only the winners is a brochure, and this product's whole claim is
    that it will say when its own memory is making things worse.

  Verified end to end on a 700-occasion run at a 40% holdout: a memory seeded
  at +16% measured +16.4% → **+67 occasions**, and the run's other memory was
  correctly excluded as unestablished rather than contributing its −16.

- **The Hub can size an experiment before an operator starts one.**
  `python -m hub.manage plan-experiment <org_id>` reads that org's own
  retrieval volume and its own resolution rate and says what holdout rate a
  10-point effect needs — or says plainly that no rate answers it in this
  window. Planning on the Hub rather than on paper matters because the Hub
  already knows the numbers; the local `--plan` has to be told them.

  `start-experiment` now warns when the chosen rate cannot answer anything at
  that org's observed volume. It still starts — the operator's decision
  stands — but it is said at the only moment the rate can be changed for
  free. An operator who learns it from the report a month later has spent the
  window on a question that was never answerable, and the fix was always a
  one-line decision taken at the start.

  An org with no volume is deliberately *not* warned: there is nothing to
  base it on, and inventing a warning would train operators to ignore the
  real ones.

- **A fleet can now set its own holdout rate, and both retrievers read it.**
  `commontrace experiment --configure --rate 0.5` writes the store's
  experiment settings; `commontrace query --experiment` and the MCP
  `retrieve` tool both read them.

  This closes a gap the previous entry created and did not fix: the product
  could compute exactly what rate a pilot needed, print it, and then offer no
  way to set it on the AI-first half of its own surface. The rate was a CLI
  flag default on `query` and a **hardcoded constant** in the MCP server, so
  an agent-driven fleet could not change it at all.

  Worse, the two surfaces could silently disagree. A person running `query
  --holdout-rate 0.5` while the same fleet's agents retrieved over MCP at 0.1
  produced a log with two randomizations pooled into one comparison — which
  `integrity.check_assignment_drift` correctly reports as INVALIDATES.
  Corrupting an experiment took nothing more than using both of the product's
  own interfaces.

  **Changing the rate rotates the salt**, and that is the point rather than a
  side effect. Assignment is `hash(lesson, occasion, salt) < rate`, so a new
  rate re-randomizes every occasion: the assignments before and after are two
  different experiments, and pooling them lets one occasion sit in opposite
  arms. Rotating makes that explicit instead of silent — and the salt is
  derived from the moment it was set, because the first question when two
  appear in one log is which came first. Honouring a new rate under the old
  salt is exactly the corruption the drift check exists to catch, and a
  product should not offer it as a command.

- **`commontrace experiment --plan` designs the experiment before you run
  it.** The failure it prevents is expensive and silent: a fleet runs a
  30-day pilot at the default rate and the report on the last day says "not
  enough data yet". The occasions are spent, the window is gone, and the only
  fix — a wider holdout — had to be applied on day one.

  ```
  $ commontrace experiment --plan --occasions 240 --detect 0.15
  To detect an effect of 15% against a 78% baseline at 80% power:
  - 119 observations in EACH arm.
  - At a 10% holdout that is 1,190 occasions.

  240 occasions can answer this, but not at 10%. Set the holdout rate to 50%.
  ```

  It reads the store's own observed baseline where there is one and falls
  back to 50% — where the variance peaks, so a plan built on no data cannot
  understate the sample. It names the rate a given budget needs, says plainly
  when **no rate can answer it** (the most useful answer, and the one worth
  having before spending the window rather than after), exits non-zero in
  that case so a script can act on it, and states the cost of a wider holdout
  rather than selling the upside alone: that share of the work runs without
  its memory for the length of the experiment.

  `required_n_per_arm` is the exact algebraic inverse of
  `minimum_detectable_effect`, not a search, so two functions describing one
  design cannot disagree about it — asserted across a grid of effects and
  baselines.

  **The default holdout rate is deliberately unchanged at 10%.** Changing it
  would re-randomize every experiment already running, which
  `integrity.check_assignment_drift` correctly reports as INVALIDATES. The
  guidance is loud instead of the default being silently different.

- **Customers had no interface.** `hub/admin.py` is the *operator* console —
  one vendor employee, cross-tenant, moderating the Knowledge Base — and
  until now it was the only HTML the Hub served. A paying organisation had
  an MCP tool surface and a CLI, and nothing else.

  That matters more than it sounds. The product's central claim is that it
  can prove causally, on the customer's own data, that the memory changed
  outcomes, and three rounds of work went into making that number
  trustworthy: a validity audit, a differential-attrition check, a treatment
  pinned to a content revision. All of it renders in a terminal, to whoever
  runs `commontrace prove outcomes`. The person who decides whether to renew
  does not run that command.

  `hub/console.py` serves `/app`: **Overview** (fleet, plan usage, and how
  often searches come back empty), **Proof** (the causal report with its
  validity verdict rendered *above* the effect sizes, plus the observational
  before/after clearly separated), **Memory** (the corpus, searched the same
  way agents search it, with the matched and ignored terms shown), and
  **Knowledge Base** (their proposals, consultations used, credit earned).

  Design decisions worth stating, because each rules something out:

  - **It writes no queries of its own.** Every figure comes from a function
    in `hub/crud.py` that already takes and filters on `org_id`. A
    cross-tenant leak here is the worst failure available to this product,
    and the isolation argument should rest on the one set of filters
    `test_tenant_isolation.py` already exercises rather than a second set a
    new file introduced.
  - **It is read-only, and that is the boundary rather than a limitation.**
    Everything a customer could change from a browser either alters a
    measurement or alters a shared corpus, and both already have audited,
    authenticated paths. Read-only also removes the entire CSRF surface —
    there is no state-changing request for a forged one to trigger — so the
    absence of CSRF tokens is correct rather than an oversight.
  - **Sessions are signed, not stored.** The Hub runs behind a load
    balancer, and an in-memory session table logs everyone out on every
    deploy. The API key is verified once at sign-in and **never put in the
    cookie**; only the non-secret prefix goes in, so a leaked cookie is not
    a leaked credential.
  - **Revoking a key ends the sessions it opened**, checked per request
    against the key's live state. An 8-hour window where a revoked key still
    served data would mean revocation that does not revoke — a false belief
    about the state of a credential, which is worse than no revocation.
  - **Effect sizes are withheld, not caveated, when validity is
    COMPROMISED.** On a page built to be read in a renewal conversation, a
    number on screen gets quoted and the note under it does not travel.
  - **Absent unless configured**, like `/admin`: no `HUB_CONSOLE_SECRET`, no
    routes. The secret is deliberately *not* `HUB_ADMIN_TOKEN` — one value
    authenticating the vendor and signing customer sessions means one leak
    compromises both.

- **An effect size was attached to a mutable name, and the treatment could
  change underneath it.** The validity audit shipped alongside this checks
  whether the *sample* can support an estimate. It did not check whether the
  *treatment held still* — and nothing did.

  A lesson is a file. `lesson approve`, `draft_lesson` over MCP, and a text
  editor all rewrite it in place, and the holdout log recorded the lesson by
  **slug**. So:

  - Edit a lesson on day 10 of a 30-day run and occasions 1–200 were treated
    with one rule, 201–400 with another. `analyze()` pools them into a single
    arm and reports one effect for a treatment that is an average of two, one
    of which no longer exists anywhere. This is the same defect
    `check_assignment_drift` catches one level down: there the
    *randomization* changed, here the *thing being randomized* did.
  - Finish a run, report "lesson_x HELPS +12%, p=0.01", then rewrite
    lesson_x. The number in the renewal deck now describes text that is gone,
    and nothing recorded what it used to say.

  The Hub had the identical defect on a different object: its holdout
  randomizes **traces**, and `amend_trace` rewrites a trace's title, context
  and solution in place. Adding the MCP `draft_lesson` tool made the local
  half worse rather than better — before it, rewriting a lesson mid-run took
  a person opening a file; now an agent can do it unattended as an ordinary
  part of curating.

  `commontrace/revision.py` gives a lesson content identity: a short digest
  over exactly the fields an agent *receives*. That line is what makes the
  check usable rather than noise — `uses` and `last_hit` change on **every
  retrieval**, so hashing them would flag every experiment inside a week,
  which is the false positive that teaches people to ignore a validity
  report. Provenance (`source_traces`, `hub_trace_id`), lifecycle (`status`)
  and telemetry are excluded for reasons stated per-field; whitespace is
  normalized, because a reflowed paragraph is not a different instruction.
  It is computed on read rather than stored, so it cannot go stale, needs no
  migration, and applies to every lesson that already exists.

  `commontrace/lesson_io.py` is now the only place a lesson is written —
  previously seven `frontmatter.write` call sites, each rewriting in place
  with no record of the previous content. Every content change is journaled
  to `memory/lesson_revisions.jsonl` (append-only, locked and fsynced, same
  shape and reasons as the holdout log) with the revision before and after,
  who changed it, and why. Writes that change nothing are not journaled:
  approve sets `status`, retrieval bumps `uses`, a push stamps
  `hub_trace_id` — recording those would bury the changes that matter under
  the ones that do not.

  - **`integrity.check_treatment_stability`** reports INVALIDATES when a
    lesson moved during a run, naming the lesson and both revisions in the
    order they actually happened.
  - **`commontrace lesson history <slug>`** shows what a lesson has said over
    time, who changed it and why — which is what makes that finding
    actionable rather than merely alarming.
  - Every effect report now names the **revision under test** beside the
    slug, so a number can never be silently detached from the text that
    produced it.
  - `retrieve` over MCP returns each lesson's `revision`, so an agent keeping
    its own records can join an outcome to the exact text it was given.
  - Hub: `holdout_observations.trace_revision` (migration
    `c3a71f5d80b2`, nullable, catalog-only — no rewrite, no long lock),
    stamped at assignment time from the trace's content.

  **Nothing is backfilled, on either tier.** What a lesson said at assignment
  time is unrecoverable once it has been edited, so a run with no recorded
  revisions is reported as *unchecked* — WEAKENS, not OK and not
  COMPROMISED. Stamping today's digest on those rows would assert the
  treatment was stable on exactly the runs where nobody can know, and
  reporting them as clean would let an old log read as a stable treatment,
  which is the state this exists to distinguish.

- **The causal number is now audited, and it could be confidently wrong
  before.** `commontrace/experiment.py` estimates each lesson's effect
  correctly — two-proportion tests, a 95% interval, Benjamini-Hochberg across
  lessons, underpowered comparisons kept out of the correction, an explicit
  `UNDERPOWERED` verdict so a small sample never reads as "no effect".
  Nothing in the arithmetic needed fixing.

  But `analyze()` only ever sees occasions that HAVE a recorded outcome, and
  both tiers dropped the rest before it — the Hub did it in SQL
  (`succeeded IS NOT NULL`), so nothing downstream could count what went
  missing, let alone which arm it came from. `hub/models.py` states the
  reasoning and it is correct: an unresolved observation is missing data,
  and scoring it as a failure would bias the arm that crashed more. What
  nothing checked is that excluding it is unbiased **only if both arms lose
  outcomes at the same rate** — and the withheld arm is *by construction*
  the one working without its memory, so it is the arm more likely to run
  long, escalate, or be abandoned before anyone writes up how it went. The
  treatment effect leaks into who gets measured.

  Reproduced, and it is in the test suite: a fleet of 600 occasions where
  the lesson does **nothing** — both arms succeed at exactly 50% — with the
  single asymmetry that a withheld occasion which failed often never gets
  reported. `analyze()` returns **HURTS, −12.6%, 95% CI [−20.8%, −4.4%],
  p=0.003**, significant and adequately powered. Driven through
  `commontrace experiment` on a seeded store the same defect reads
  **−17%, p=0.002**. It does not error, does not return empty, and does not
  read as underpowered. It reads as a clean, well-powered finding pointing
  the wrong way about a lesson that was fine — and the product's own
  documentation makes a virtue of reporting `HURTS`, so a customer would
  have retired it.

  `commontrace/integrity.py` is the audit, shared by both tiers for the same
  reason `holdout_io.py` owns arm assignment. Five checks, each with a
  severity that means something specific — `INVALIDATES` (a named mechanism
  is biasing the estimate), `WEAKENS` (degraded but not demonstrably biased,
  usually lost power), `OK` (checked, nothing found — stated explicitly, so
  silence is never mistaken for a clean bill):

  - **Differential attrition**, the load-bearing one. Reports the direction,
    because which arm loses data decides which way the number is wrong.
  - **Arm balance** — realized withheld share against the configured rate.
    Assignment is a deterministic hash, so a large gap is not luck; it means
    something other than that hash decided these arms.
  - **Mid-run re-randomization** — a changed salt or rate re-randomizes every
    occasion, so the log stops being one experiment and becomes two pooled
    into one comparison, with the same occasion able to sit in opposite arms.
  - **Conflicting arms** — one (lesson, occasion) in both arms at once:
    evidence for and against the same lesson simultaneously.
  - **Outcome variation** — an all-succeeded corpus produces a difference of
    exactly zero with a tidy interval, and reads as a confident null. It is
    not one; it is an outcome nothing can fail.

  The validity alpha is deliberately **looser** than the 0.05 the effect
  analysis uses (0.10). The two tests are asked in opposite directions: for
  an effect a false positive is the expensive error, so the bar is high; for
  a validity check a false *negative* is — missing a real bias means
  publishing a wrong number — so the bar is lower. A flagged experiment
  costs someone a look; an unflagged broken one costs the claim.

- **A power projection, because "underpowered" on day 30 is a spent pilot.**
  `experiment` already said a lesson was underpowered and how many
  observations each arm needed. What decides whether a pilot lands is
  *when*. Each lesson now reports how far it is from answerable and, where
  the log is dated, roughly what date at the current accrual rate. The
  control arm almost always binds and the reason is arithmetic rather than
  bad luck: at a 10% holdout it takes ~100 occasions to put 10 in the
  control, so a run reaches an answer about ten times slower than its
  occasion count suggests. The projection says so and names the rate that
  would fix it. Assignments now carry a timestamp; a log written before this
  simply reports no accrual rate rather than failing.

  Wired everywhere the number is read: `commontrace experiment` (validity
  above the table, never in a footnote under it — a report that leads with a
  significant number and caveats it underneath is exactly how a broken one
  gets quoted), `commontrace pilot`, `prove outcomes`, the Hub's
  `fleet_outcomes` response under `causal.integrity`, and a new
  `experiment_status` tool on the local MCP server so an agent can check
  whether the run it is feeding will ever answer.

  Three things it deliberately does not do. It does **not correct** the
  estimate — nothing can recover an outcome that was never recorded, so a
  compromised run gets a refusal to report rather than a fixed number. It
  **cannot detect contamination** — an agent that uses a lesson it was told
  to withhold leaves no trace and biases the effect toward zero; that is
  honoured by the client or not at all, and every rendered report says so
  rather than staying silent, because silence would read as coverage. And
  **absence of a finding is not proof of validity**: these catch the
  failures that leave a trace in the assignment log, which is not every
  failure.

- **The local tier now speaks MCP, so an agent no longer needs a terminal to
  use its own memory.** `commontrace serve` exposes the local store over MCP
  stdio: `retrieve`, `capture`, `propose_lessons`, `list_lessons`,
  `get_lesson`, `draft_lesson`, `approve_lesson`, `reject_lesson`,
  `store_status`.

  Every one of those steps existed already and every one was argparse-only,
  which meant the only agents that could use their own memory were the ones
  that happen to have a shell. The README's claim is "works with any agent
  fleet — code, support, sales, HR, marketing", and it held for the first
  one: a support agent embedded in a helpdesk, a sales agent inside a CRM,
  an ops agent in a runbook tool cannot shell out, so none of them could
  retrieve a lesson, record what happened, or curate anything. They could
  talk to a remote Hub and do nothing with their own store.

  Nothing here reimplements the protocol. `retrieve` uses the loader and
  ranker `commontrace query` uses; `capture` and `propose_lessons` run the
  real `capture` and `distill` commands through their own argparse parsers,
  in-process. That is not fastidiousness — the first draft *did*
  reimplement the distill loop and got the candidate naming wrong (the slug
  already carries its `lesson_` prefix), writing `lesson_lesson_candidate_…`
  files that every other command then failed to resolve. Routing through the
  parser also caught a hand-built namespace spelling `--frustration` as
  `frustration_signal`: accepted in silence, recorded nowhere.

  The randomized holdout came with the same risk and got the same treatment.
  Arm assignment now lives in one place (`commontrace/holdout_io.py`) that
  both retrievers call, salt included — two implementations of a randomized
  assignment is two chances to bias the causal number the whole experiment
  exists to produce. An occasion gets the same arm whichever surface asked.
  The loop is measurable end to end from MCP alone: retrieve with an
  `occasion_id`, capture the outcome under the same id, and
  `commontrace experiment` reports the arms.

  Two design points worth stating, both tested:

  - **No authentication, because there is no boundary to authenticate.** The
    client spawns the process and talks to it over its own stdin/stdout —
    no port, no listener, nothing for another program on the machine to
    connect to. It touches `memory/` with exactly the permissions of the
    agent that launched it. A token here would protect nothing and imply a
    boundary that does not exist. (The Hub is multi-tenant and
    network-reachable, and is authenticated on every call.)
  - **An agent may approve its own lesson, but the gate is real.**
    `approve_lesson` enforces the same scaffolding refusal
    `commontrace lesson approve` does — an active lesson is injected into
    every later retrieval verbatim, so activating one whose rule still reads
    `TODO:` teaches the fleet nothing, displaces a real lesson, and counts as
    coverage in the reports a customer reads. It also records **who**
    approved it, so an agent-approved lesson stays distinguishable
    afterwards. `serve --no-approval` removes the tool entirely — absent from
    the listing, not present and refusing, so the agent never plans around a
    call it cannot make.

  `commontrace install` now writes `commontrace.local.mcp.json` for every
  MCP-capable target, alongside the existing Hub template. Both are usually
  wanted and they are not interchangeable: an agent with only the Hub entry
  can search what the Knowledge Base has published and cannot read or write
  a single lesson of its own. The generated file carries the **resolved**
  store root, because an MCP client launches the server with a working
  directory of its own choosing and a relative root silently resolves to a
  different, usually empty, store — a failure that presents as "no lessons
  matched" rather than as an error.

  One transport hazard is worth recording, since it is invisible until it
  bites: **on stdio, the process's stdout is the MCP wire.** The command
  modules this server reuses print — `capture` prints the path it wrote, the
  loaders warn about an unreadable file — and a single stray line lands
  mid-frame and breaks the client's parser for the whole session, not for
  that one call. The symptom looks like a server crash triggered by
  something as ordinary as one malformed trace file. Every reused command is
  therefore run with stdout captured, and `tests/test_mcp_server.py` spawns
  a real `commontrace serve` subprocess with a deliberately malformed lesson
  in the store and speaks MCP to it, because nothing short of that proves
  the framing survives.

- **The Knowledge Base is now operable from the console** — the review queue
  that decides what goes into the one surface where anything crosses an org
  boundary.

  Orgs never exchange anything with each other, and that is designed rather
  than incidental: a fleet's traces stay private to that fleet, and
  `hub/commons.py` records why direct org-to-org sharing was retired
  (adverse selection — the most valuable lessons are the most proprietary,
  so voluntary contribution biases toward filler). The exchange that does
  exist has two directions and a person in the middle: an org **consults**
  the Knowledge Base by sending a MinHash signature (never its text), and an
  org **proposes** an entry that sits in a separate table no commons query
  reads, invisible to everyone, until an operator accepts it.

  That review step was CLI-only, which is the wrong place for it: a
  Knowledge Base is only as good as its queue, and a queue that can only be
  worked from a terminal does not get worked. The console now shows each
  proposal with its rationale, its context, its solution and the org that
  sent it, and accepts or declines it in place. Accepting publishes under the
  operator's org and permanently credits the proposer's query allowance;
  declining awards nothing, which is the adverse-selection defence. Published
  entries can be retracted and restored from the same page.

  **This changed the console's rule from "read-only" to reversibility**, and
  the new line is the honest one:

  | Class | Where |
  |---|---|
  | Reversible moderation (accept/decline, retract/restore) | the console |
  | Irreversible or credential-bearing (`purge-org`, `issue-key`) | the CLI only |

  Because authentication is HTTP Basic — which a browser re-sends on a
  cross-site form POST — every mutating endpoint requires a CSRF token that
  is an HMAC of the action *and* its target under the admin secret, so a
  token minted to decline one proposal cannot approve another. Cross-site
  posts are refused where `Sec-Fetch-Site` reports them. Publishing requires
  `HUB_OPERATOR_ORG_ID` and **fails closed** without it: putting a customer's
  id on Knowledge Base content is the one mistake this boundary exists to
  prevent, and not one a form should be able to make. Every decision writes
  the same audit row the CLI writes, under the actor `operator-console`.

  Verified by driving it in a real browser: a proposal that was genuinely
  substrate was accepted and a proposal that was one org's own policy was
  declined; the published entry came out owned by the operator org, the
  proposer earned credit for the accepted one and nothing for the declined
  one, and the entry then came back through `commons_search` for a client
  that consulted it by signature.

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

### Changed

- **A proposed lesson now carries the evidence needed to write it.** This is
  the throughput limit on the whole product, and `distill` was making it as
  expensive as possible.

  Found by running the cold-start journey a new customer runs: import 60
  support tickets, `taxonomy`, `distill`. The first two steps are good — 60
  tickets in, five recurring failure families correctly identified, all
  reported as uncovered gaps. The third produced five candidates that looked
  like this:

  - the description was the cluster's shared **terms** —
    `"Candidate: 12 traces show a repeated pattern around: anywhere, byte,
    csv, customer, empty"` — so a review queue was a list of
    indistinguishable word bags. `description` is also a ranked retrieval
    field, so those were the tokens the candidate matched on.
  - the evidence section printed **one line per trace**, context only, with a
    UUID on each — the same paragraph twelve times.
  - **the solution text was nowhere in the file.** The one thing anyone needs
    in order to write the Rule. Curating a lesson meant opening twelve trace
    files to find what had actually worked.

  That chain has a cost at every link: coverage stays low because curating is
  expensive, retrieval returns nothing because coverage is low, and the
  randomized experiment stays underpowered because there is nothing to
  measure.

  Now: the description is the **medoid** trace's own title — a sentence a
  person already wrote about this exact failure, picked as the most typical
  member of the cluster rather than whichever sorted first, and deterministic
  so two identical runs propose identical text. The evidence section groups
  repeats with counts and shows **both** the situation and what resolved it.

  The most useful case is the one that was completely invisible before: when
  a cluster has more than one distinct resolution, all of them are listed and
  the candidate is flagged — *"3 different resolutions for the same symptom.
  That usually means this is more than one problem — consider splitting the
  candidate."* One symptom with three root causes written up as a single rule
  produces a lesson that fires on cases it cannot help, and nothing surfaced
  that before.

  **The TODOs stay.** `applies_when`, `do_not_apply_when` and the Rule are
  judgements, and filling them from a term-frequency count would push
  fabricated text past the scaffolding guard that exists to stop exactly
  that. Proposing better evidence is honest; proposing the conclusion is not.

  The description is one real example standing in for a cluster and is
  labelled as a proposal, because it is one — a reviewer still has to
  generalise it, and a title carrying instance-specific detail (a ticket
  number, a customer name) is a reason to edit it rather than to go back to
  a word list.

  Reaches the MCP surface for free: `propose_lessons` runs the real `distill`
  command, so an agent calling `get_lesson` on a candidate now reads what
  worked without looking up a single trace.

- **The arm-balance check flagged one sound experiment in ten.** It shared
  the attrition check's alpha (0.10), and a two-sided test at alpha=0.10
  flags a *correct* randomizer about 10% of the time — at every n; that is
  what an alpha is. Since this check runs on every experiment, one valid run
  in ten would have been reported COMPROMISED for nothing, which is the
  "cries wolf" failure the module's own comments warn about.

  The two checks are looking for effects of different size, so they now have
  different alphas. Attrition is a gradient where a 10-point reporting gap
  between arms matters. Arm balance is not: assignment is a deterministic
  hash compared against a threshold, so it is either being applied or it is
  not, and a broken assigner misses by many standard deviations rather than
  by a couple. At `ARM_BALANCE_ALPHA = 0.001` a correct randomizer is flagged
  ~0.1% of the time and every realistic breakage — a rate 2× or 5× off, one
  arm always — is still caught. Both properties are measured in
  `tests/test_integrity.py` rather than argued.

  Found by a test that failed about one run in fifteen under random
  ordering.

- **`commontrace experiment --strict` now also fails a compromised run.**
  The flag means "stop the build if the memory is making things worse", and
  a biased comparison cannot answer that in either direction. Passing it
  silently converts "we could not tell" into "we checked and it was fine",
  which is the one claim nobody should make.

### Fixed

- **Every hub-tests CI job failed at collection, on all three Python
  versions, and the local suite passed the whole time.** `install_cmd` grew a
  module-level `from commontrace import mcp_server` to read a tuple of tool
  NAMES; `mcp_server` pulls in the retrieval stack (`evidence_io` →
  `frontmatter` → `yaml`). The hub-tests job installs `hub/requirements.txt`
  only — **no PyYAML**, because the Hub server does not need it — and
  `hub/tests/test_install_template_surface.py` imports `install_cmd`. Result:
  `ModuleNotFoundError: No module named 'yaml'` for a package nothing in
  `hub/` uses.

  Invisible locally, because a development machine has PyYAML. The fix is the
  coupling, not the symptom: `commontrace/mcp_tools.py` holds the names and
  imports nothing, `install_cmd` reads it, and `mcp_server` re-exports the
  same objects so no caller changes and the two cannot drift.

  The guard that would have caught it now exists in the file that broke: a
  subprocess with `yaml` blocked, asserting `install_cmd` still imports —
  which is the only way to reproduce the Hub's environment from a machine
  that has the package.

- **The local causal report pooled every randomization the store had ever
  run.** The Hub has always scoped its analysis to the current salt, in SQL.
  The local report did not, so the first time anyone changed their holdout
  rate the report became permanently invalid — it said so, via a drift
  finding, which is better than silence and worse than not doing it. It now
  scopes to the current randomization, names how many assignments were
  excluded and why, and `--salt <salt>` reads an earlier run.

  Scoping introduced a way to lose an entire experiment history on upgrade,
  caught by an existing test: a log line written before salts were recorded
  parses with an empty salt, which matches no configured randomization, so a
  store whose log predated the field would have gone from "here are your
  results" to "none under the current randomization" with no change on the
  customer's side. An absent salt is now read as the default randomization,
  which is what it was — back then there was only one.

- **A 240-occasion pilot with a real +25pp effect reported
  `NO_MEASURABLE_EFFECT`.** Found by running the customer journey to the end
  — import 36 tickets, curate three lessons through the MCP tools, run 240
  occasions with the holdout, read the report.

  The gate separating "not enough data yet" from a reported result was
  `min_arm = 10`, which is a floor on *running* the test, not a power
  criterion. At 10 observations per arm against a 60% baseline the minimum
  detectable effect is **61 percentage points**. Any run clearing that floor
  and finding nothing was labelled `NO_MEASURABLE_EFFECT` — which a customer
  reads as "the memory does not work" — when the honest statement is "this
  design could not have seen anything short of a 61-point swing." Even at
  1,000 occasions at the default 10% holdout the MDE is 19%, larger than
  almost any real product effect.

  The report did print the MDE beside the verdict. That is not enough: the
  verdict is the thing that travels.

  A null is now reported as `UNDERPOWERED` unless the design could actually
  have detected an effect worth acting on (`DEFAULT_PRACTICAL_EFFECT`, 10
  points, configurable with `--detect`). The asymmetry is deliberate and is
  what makes this correct rather than merely cautious: **a significant result
  at small n is still a detection** and keeps its `HELPS`/`HURTS` verdict —
  power governs how to read a null, not a finding. Benjamini-Hochberg still
  runs over every comparison that met the floor, because *m* must be the
  number of tests actually run and must not depend on their results.

  Same defect class as the last three rounds — a confident verdict where the
  honest answer is "cannot tell" — and this one was in the single number the
  entire business case rests on.

- **Hub integrity findings used the local tier's vocabulary.** The two tiers
  randomize different objects — the local tier withholds *lessons*, the Hub
  withholds *traces* — and the shared checks reported both as "lesson". A
  Hub customer reading "1 lesson(s) were edited" about their own traces has
  been handed the wrong tier's words and will go looking for an object they
  do not have. `integrity.audit(..., unit=...)` threads the right noun
  through, and the Hub passes `trace`. Invisible to any test that only
  asserts severities, which is why it was found by reading a rendered page.

- **`commontrace doctor` could be killed by its own optional-dependency
  probes.** `importlib.util.find_spec` walks `sys.meta_path`, so any import
  hook installed in that interpreter gets to raise inside it — and an
  unhandled exception from the `numpy` or `sentence_transformers` probe took
  down the whole report *before* the checks that would have named the real
  problem. That is the worst possible time for it: `doctor` is the command
  someone runs when the environment is already broken. Every probe is now
  guarded, and an unanswerable one reads as "not installed", which is
  conservative — each absent branch is an INFO line with an install hint,
  never a failure. `doctor` also now reports whether the MCP SDK is present,
  since `commontrace serve` without it fails inside a subprocess an MCP
  client spawned, where nobody sees the traceback.

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

[Unreleased]: https://github.com/varma61923/commontrace-v2/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/varma61923/commontrace-v2/releases/tag/v2.0.0
