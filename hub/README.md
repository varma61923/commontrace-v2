# CommonTrace Hub

The server side of the "Hub" conformance tier in
[`protocol/PROTOCOL.md`](../protocol/PROTOCOL.md#5-store-two-conformance-tiers).

Until this was built, `commontrace/commands/sync_cmd.py` only printed
instructions for a Hub that didn't exist anywhere in this repository or any
deployed service (see the repo's Phase 0 resolution: **(B) no Hub server
exists**). This directory is that server, plus the client wiring in
[`commontrace/hub_client.py`](../commontrace/hub_client.py) that makes
`commontrace sync` actually work end-to-end.

## What it is

An MCP server over streamable-HTTP. Six tools match `PROTOCOL.md` §5's
names and semantics exactly:

`search_traces(query, tags)` · `contribute_trace(title, context_text, solution_text, tags, agent_type)` · `get_trace(id)` · `vote_trace(id, vote, feedback_tag, feedback_text)` · `amend_trace(id, ...)` · `list_tags()`

Thirteen more are Hub-specific and outside the protocol. `delete_trace(id)`
permanently deletes one of your own traces (self-service, immediate,
irreversible); `request_account_deletion()` / `confirm_account_deletion
(confirmation_token)` / `cancel_account_deletion()` do the same for your
entire organization, split into two differently-named calls with a
mandatory delay between them so a single compromised API key cannot wipe
an org's whole history with no chance for anyone to notice -- see
"Self-service deletion" below. `commons_overlap(failures)` and
`commons_search(question)` query the CommonTrace Knowledge Base, a single
corpus the operator authors and curates (`hub/manage.py commons_seed`,
plus accepted community submissions -- see "Community submissions"
below). `submit_kb_entry(title, context_text, solution_text, tags,
agent_type, rationale)` proposes a new entry for operator review and
`list_my_kb_submissions()` checks its status; neither publishes anything
by itself, so no customer's own trace is ever visible to any other org
without an operator's own review-submission action deciding it should be.
`account_usage()` reports the caller's own plan and meter, and
`fleet_outcomes()` answers whether the fleet's own recorded outcomes have
actually improved since its baseline window -- both org-scoped like the
six protocol tools, both unmetered, and both reading nothing but the
caller's own data. `holdout_assign(trace_ids, occasion_id)` and
`record_occasion_outcome(occasion_id, succeeded)` run a randomized
holdout -- the only design here that supports a *causal* claim about
whether your memory is helping; see "The randomized holdout" below.
`value_delivered(value_per_occasion)` turns that causal effect into a
count -- `effect x times injected`, established memories only, HURTING ones
subtracted rather than dropped, nothing at all if the experiment is
COMPROMISED -- and attaches your own supplied rate to it if you pass one;
this is this product's pricing basis (STRATEGY.md §11.5), so it is worth
knowing it exists even though it reads like a footnote to `fleet_outcomes`.
`working_set(budget_chars)` is the other end of that same instrument: it
returns only the memories whose effect is already *established* as helping,
packed to a character budget, as one block an agent pins to its system
prompt once per session instead of paying for on every query. Because the
block does not change between turns, a provider's prompt-prefix cache can
serve it; a memory that were both pinned and still under randomization
would be injected on every occasion and destroy its own control arm, so a
trace is either being randomized or graduated, never both.
`hub/smoke.py` pins the tool surface, so a tool
appearing or disappearing fails a post-deploy check rather than
surprising a client.

The four Knowledge Base tools can be removed from the surface entirely with
`HUB_COMMONS_ENABLED=false` (hub/config.py) -- an unknown-tool error to any
client that tries, not a per-call refusal, so the guarantee holds for a
deployment even if every org on it forgets the feature exists. See
`hub/DEPLOYMENT.md` §13 for an internal-only deployment that wants this.

Any MCP-capable agent (Claude Code, Cursor, Devin, Windsurf, a generic MCP
client) can attach with a plain config block and an API key — no
CommonTrace-specific SDK, per `PROTOCOL.md` §8.

## Architecture at a glance

```
hub/config.py      env-driven settings, no unsafe defaults
hub/models.py      SQLAlchemy 2.0 ORM: Organization, ApiKey, Trace, Vote, TraceRelation, UsageCounter
hub/db.py          async engine/session plumbing
hub/schema_validation.py   loads protocol/schemas/*.json from disk, validates against them
hub/auth.py        API-key hashing/verification/rotation/expiry (HMAC fast path, argon2 fallback) + request-scoped org_id
hub/abuse.py       size limits, per-org rate limiting, a spam + content-safety heuristic -> quarantine
hub/audit.py       append-only audit-log writes (who did what, no secrets, no content)
hub/observability.py  JSON logging, request-id correlation, /healthz + /readyz + /metrics
hub/admin.py          read-only operator console at /admin (off unless HUB_ADMIN_TOKEN is set)
hub/console.py         customer console at /app (off unless HUB_CONSOLE_SECRET is set) -- read-only
                        over this Hub's own data; can also send a browser to Stripe (see billing.py)
hub/signup.py       public, self-serve org creation at /signup (off unless HUB_SIGNUP_ENABLED is set)
hub/billing.py      self-serve Stripe upgrades: Checkout/Billing Portal + the webhook that applies them
hub/plans.py       entitlements: what each plan grants, and the credit contributors earn
hub/outcomes.py    before/after fleet outcome measurement (observational; statistics imported from commontrace/experiment.py)
hub/bench_scaling.py  does serving one customer get more expensive as their corpus grows? (see SCALING.md)
hub/crud.py        every tool's actual query logic -- ALWAYS org_id-scoped in SQL
hub/server.py      thin MCP wiring: auth middleware + tool handlers that call crud.py
hub/main.py        `python -m hub.main` -- run the server
hub/manage.py       `python -m hub.manage <cmd>` -- org/API-key operator CLI
hub/alembic/        migrations (see "Running locally" below)
hub/DEPLOYMENT.md   running it for real: probes, scaling, backups, security checklist
hub/SCALING.md      measured cost-to-serve vs. corpus size, per read path
hub/tests/          pytest suite, including test_tenant_isolation.py
```

`hub/crud.py`'s module docstring explains the layering in detail. The short
version: `hub/server.py` never touches SQL, `hub/crud.py` never touches MCP
protocol shapes, and every read/write in `crud.py` that touches the `traces`
table puts `org_id` in the SQL `WHERE` clause itself — never "fetch rows,
then filter in Python." That's what makes tenant isolation a property of the
query layer, not an application-level convention someone can forget.

For a real deployment (containers, probes, scaling, backups, security
checklist) see **[hub/DEPLOYMENT.md](DEPLOYMENT.md)**. The rest of this
file covers running it from a checkout and the design decisions behind it.

## Running locally

Requires a real Postgres instance (this was developed and tested against a
local Postgres 16). For a containerized stack instead, see
[DEPLOYMENT.md](DEPLOYMENT.md).

```bash
pip install -r hub/requirements.txt

createdb commontrace_hub          # or your usual DB provisioning
cp hub/.env.example hub/.env      # fill in HUB_DATABASE_URL etc.
export $(grep -v '^#' hub/.env | xargs)   # or use your process manager

python -m alembic -c hub/alembic.ini upgrade head

python -m hub.manage create-org "Acme Corp"
python -m hub.manage issue-key <org_id_from_above> 90   # prints the raw key ONCE; expires in 90 days

python -m hub.main   # serves streamable-HTTP MCP on HUB_HOST:HUB_PORT/mcp
```

Point any MCP client at `http://<host>:<port>/mcp` with
`Authorization: Bearer <api-key>`. `GET /healthz` (liveness), `GET /readyz`
(readiness, checks the database) and `GET /metrics` (Prometheus) are
unauthenticated — see [DEPLOYMENT.md §4](DEPLOYMENT.md#4-health-probes) for
why the probes are separate and which to attach to each.

Set `HUB_ADMIN_TOKEN` to also serve a **read-only** operator console at
`/admin` (HTTP Basic; unset means the routes do not exist). It shows every
org against its plan, key state and expiry, quarantined traces, retrieval
miss rate, audit history and the Knowledge Base queue — and for anything
that changes state it shows the `hub.manage` command rather than doing it.
That is deliberate: see `hub/admin.py`.

Set `HUB_CONSOLE_SECRET` to also serve a second, **read-only** console at
`/app` — not for you, for your **customers** (unset means these routes do
not exist either, same as `/admin`). Where the operator console is
cross-tenant and moderates, this one is scoped to a single organization
and cannot change any state at all. A customer signs in with the same API
key their agents already authenticate with; the Hub verifies it once and
never stores it, then hands back a signed, `HttpOnly` session cookie
scoped to that org, checked against the key's live/revoked state on every
request — so revoking a key ends the browser sessions it opened, not just
future MCP calls. From there they get four pages, all reading through the
same `org_id`-scoped functions in `hub/crud.py` as every other Hub
surface, rather than a second set of queries to keep tenant-isolated: an
overview of what their fleet has captured and how it sits against plan; a
proof page where the randomized holdout's validity verdict renders
*above* the effect sizes it qualifies, because a report that leads with a
significant number and caveats it underneath is how a broken one gets
quoted; their own corpus, searched the way their agents search it; and
their Knowledge Base proposals and the query credit those proposals
earned. Everything that changes state in THIS HUB'S OWN DATA — capturing a trace,
running the experiment, proposing to the Knowledge Base — still goes
through MCP or the CLI, where it is authenticated and audited; nothing a
browser does at `/app` writes to `Organization`, `Trace`, or any other row
here directly. The one exception carries its own trust boundary rather
than weakening this one: an "Upgrade" click sends the browser to a
Stripe-hosted Checkout/Billing Portal page (`hub/billing.py`), and this
Hub's own `Organization.plan` only ever changes later, from Stripe's own
signed webhook call — never from the browser request itself. The console
still carries no CSRF token: its session cookie is `SameSite=Strict`, so a
forged cross-site request arrives with no session at all and is turned
back at sign-in, the same defense that already covered every other route
here. See `hub/console.py`'s and `hub/billing.py`'s module docstrings for
the rest of that reasoning. `HUB_CONSOLE_SECRET` is deliberately a
separate value from `HUB_ADMIN_TOKEN`, too — one is your operator
credential, the other signs customer sessions, and collapsing them into
one secret would mean a single leak compromises both surfaces at once
(see [DEPLOYMENT.md](DEPLOYMENT.md)).

Set `HUB_SIGNUP_ENABLED=true` to also serve a public, unauthenticated
`/signup` route: a visitor creates their own free-plan org and first API
key with no operator involved (unset means, as with `/admin` and `/app`,
the route does not exist). See `hub/signup.py`'s module docstring for what
this deliberately does not do (no email verification) and how it's kept
from becoming an abuse vector (a tight per-address rate limit, a honeypot
field, and the free plan's own storage/agent/query ceilings either way).

Set `HUB_STRIPE_SECRET_KEY`, `HUB_STRIPE_WEBHOOK_SECRET`, and at least one
of `HUB_STRIPE_PRICE_TEAM`/`HUB_STRIPE_PRICE_SCALE` to let a signed-in
customer upgrade themselves via Stripe Checkout, and to keep this Hub's
own plan column in sync with what Stripe actually charged via
`/billing/webhook` (also off, and unregistered, until the webhook secret
is set). See `hub/billing.py`'s module docstring for why there's no Stripe
SDK dependency and why an already-subscribed org is routed to Stripe's
Billing Portal rather than through Checkout a second time.

### Running the tests

```bash
createdb commontrace_hub_test
export HUB_TEST_DATABASE_URL=postgresql+asyncpg://.../commontrace_hub_test
python -m pytest hub/tests/ -q
```

This is a separate invocation from the main `python -m pytest tests/ -q`
suite on purpose: the Hub's dependency footprint (SQLAlchemy, asyncpg,
Alembic, argon2-cffi, a running Postgres) is much heavier than the
PyYAML-only CLI core, and `tests/` should keep working with nothing but a
bare `pip install -e .`.

## Design decisions worth reading before you extend this

### Tenant isolation vs. the CommonTrace Knowledge Base

The brief this server was built from calls tenant isolation "the
highest-priority requirement" and specifies a hard test: as `org_a`, zero
rows belonging to `org_b` may ever appear in any tool's responses,
and `get_trace` on a known `org_b` id must 404, never 403 (never confirm the
id exists). `hub/tests/test_tenant_isolation.py` enforces exactly that.

**Two independent layers now enforce it.** The first is the one that does
the work on every normal path: every read in `hub/crud.py` filters by
`org_id` in the SQL `WHERE` clause. The second is Postgres row-level
security, which exists because the first is a *discipline* — it holds for
every query somebody remembered to write correctly, and one omitted
predicate in one future query is a cross-tenant read no existing test
would catch.

`hub/db.py:session_scope` issues
`SELECT set_config('app.org_id', :org, true)` at the start of every
transaction, taking the org from the contextvar the auth middleware
already sets, so no call site has to remember to pass it. The policies
(`hub/alembic/versions/d5c8b3a91e77_row_level_security.py`) then make a
missing `WHERE` return **zero rows instead of another tenant's**.
`hub/tests/test_row_level_security.py` proves it by running the mistake
itself — a query with no `org_id` predicate at all — and includes a
control asserting that same query really does leak when RLS is off, so
the test cannot pass for the wrong reason.

Operator paths (`hub/manage.py`, the benchmarks, alembic) never set the
contextvar and are treated as unscoped, exactly as before. Knowledge Base
entries stay readable across orgs, because that is what the Knowledge Base
is; the write policy grants no such latitude, so no caller can create or
alter a row in another org's name.

**A policy only counts if the connecting role is subject to it.** Postgres
skips every policy for a superuser or a `BYPASSRLS` role, silently — no
error, no log line — which makes "installed but inert" a worse state than
"not installed": a guarantee an operator believes in and does not have.
This repo shipped that state, because the Postgres image makes
`POSTGRES_USER` the cluster superuser and `docker-compose.yml` served as
exactly that role. Two changes close it: the compose stack now creates a
`NOSUPERUSER`/`NOBYPASSRLS` runtime role that owns nothing
(`hub/postgres-init/10-runtime-role.sql`) and serves as that, keeping the
owner for migrations only; and `hub/db.py:check_row_level_security`
**refuses to start** when it finds policies installed that the connecting
role would bypass, unless `HUB_ALLOW_RLS_BYPASS=true` says so deliberately
(`HUB_REQUIRE_RLS=true` is the stronger form — policies must be present and
enforced). An unreachable database at boot still only warns: "cannot
determine" is not "determined to be unsafe". See `hub/DEPLOYMENT.md` §2.1.

RLS is worth not over-trusting even when it does bite, and the migration
lists its limits in full: FK and `UNIQUE` checks run outside the policy and
remain a side channel, RLS does not sanitise query logs, views need
`security_invoker = true`, and logical replication ignores policies unless
per-publication filters are configured.

An earlier design opened a second door alongside the six protocol tools:
an org could opt a trace into a shared corpus other orgs' queries could
match against (`share_trace`/`unshare_trace`). That design is retired --
it does not make sense for orgs to share their IP and data with each
other, and it has an adverse-selection problem with no fix (why would an
org contribute knowledge that might help a competitor?). See
`hub/commons.py`'s module docstring and `hub/plans.py` "why there is no
org-to-org sharing here" for the full reasoning.

**What replaced it.** The CommonTrace Knowledge Base: a single corpus the
*operator* authors and curates, closer to a vendor-maintained Stack
Overflow or wiki than to anything org-to-org. There is no tension left to
resolve, because there is no second party's data in the picture at all.

That corpus does grow from community contribution now -- `submit_kb_entry`
lets an org propose an entry -- but proposing is not publishing. A
submission lands in its own table (`KnowledgeBaseSubmission`), invisible
to every commons query and to every other org, and stays there unless an
operator's own `review-submission` action accepts it. See "Community
submissions" below for why review (not opt-in) is what keeps this from
reopening the adverse-selection problem the retired design had.

**The walls.** Every one of the six original read paths —
`search_traces`, `get_trace`, `vote_trace`, `amend_trace`, `list_tags`,
`contribute_trace` — is still unconditionally scoped to the caller's own
`org_id`, and `hub/tests/test_tenant_isolation.py` passes unchanged.
Nothing about the Knowledge Base loosened them.

**The boundary.** `Trace.commons_source == "seed"` is the only content any
Knowledge Base query will ever match against:

- `hub/manage.py commons_seed` (bulk load) and `hub/crud.py:review_kb_submission`
  (one accepted community submission at a time) are the only two things
  that ever write a `commons_source == "seed"` row, both under an operator
  org and both reachable only from `hub/manage.py`. No customer-facing
  tool can write this column, directly or by triggering it automatically.
- Quarantined content cannot enter — that would propagate exactly what
  quarantine exists to contain.
- Both Knowledge Base queries, `commons_overlap` and `commons_search`,
  filter explicitly on `commons_source == "seed"` in their SQL, not merely
  on the absence of a customer-facing sharing tool -- a stronger guarantee
  that holds even against a hypothetical future bug
  (`hub/tests/test_commons.py::test_a_shared_row_that_is_not_seed_sourced_is_still_invisible`
  pins this).

The boundary being drawn on *content* is **substrate knowledge is
Knowledge-Base material; any customer's business logic is theirs alone**
(`STRATEGY.md` §4). That call belongs to the operator authoring the
corpus, not to any customer, because no customer's own trace ever reaches
it to need a call made about it.

**Why it is signatures-in, always.** The question an org wants answered is
"how many of the failures my fleet keeps hitting has the Knowledge Base
already solved?" Answering it must not require uploading those failures.
So the client MinHashes locally and sends only signatures; what comes back
is drawn only from the operator-curated corpus. Stated limitation, not
glossed: MinHash is not a cryptographic privacy guarantee — a party who
can guess a candidate string can test whether it is present. Private set
intersection is the real fix and is a named follow-up, not a quiet
assumption.

One consequence worth knowing: `vote_trace` is the one exception to "every
read path stays org-scoped" -- it also reaches Knowledge Base entries
(`commons_source == "seed"`) regardless of which org is voting, since
`trust` is a signal surfaced to every org an entry matches for. It still
cannot reach another org's own private trace; `amend_trace`/`get_trace`
remain fully org-scoped.

### Community submissions: review, not opt-in

`submit_kb_entry` reopens a contribution channel the retired `share_trace`
design also had, and it would reopen the same adverse-selection problem
(`STRATEGY.md` §3) if contribution alone earned the reward: an org keeps
its genuinely valuable lessons and submits generic filler to collect
`bonus_commons_queries`. The fix is where the credit attaches.

`share_trace` credited the act of sharing (later, hits delivered).
`submit_kb_entry` credits nothing by itself -- it writes a
`KnowledgeBaseSubmission` row with `status='pending'`, a table entirely
separate from `Trace`, unreadable by `commons_overlap`/`commons_search`
and invisible to every other org's tools. Credit
(`plans.SUBMISSION_ACCEPTANCE_CREDIT`, permanently added to
`Organization.bonus_commons_queries`) is granted only by
`hub/crud.py:review_kb_submission`, called only from `hub/manage.py
approve-submission` -- an operator reading the content and judging it
worth publishing. A rejected or still-pending submission earns nothing, so
submitting filler to farm allowance is not a viable strategy the way it
was under `share_trace`'s "credit for sharing" rule.

This does not make an org's underlying incentive to withhold its best
lessons disappear -- nothing could. What it changes is what accumulates:
self-selected for being non-competitive enough to pass a human's review,
the same as a Stack Overflow answer or a Wikipedia edit, rather than
whatever volume of "sharing" a credit formula alone would reward.

`hub/manage.py list-submissions` is the review queue; `kb-stats` reports
the pending/approved/rejected funnel alongside the corpus's own
content-quality numbers.

### The randomized holdout: the only causal instrument here

"Fleet outcomes" below compares a fleet against its own past. That cannot
separate this product's contribution from anything else that changed in
the same window, and it says so on every response. This can.

`holdout_assign(trace_ids, occasion_id)` decides, per (trace, occasion),
whether to inject the memory or deliberately **withhold** it, and records
the arm. `record_occasion_outcome(occasion_id, succeeded)` closes the
loop. The comparison is then two arms of the same fleet in the same
window, differing only by the treatment — so "what else changed that
quarter?" has an answer, and the answer is "nothing, by construction".

`STRATEGY.md` §11.3 names causally-measured memory as the **entire moat**,
and §13.2 calls running this *"the cheapest falsifier in the document"* and
says to run it first. Both were already true of
`commontrace/experiment.py` — which works against a local file store.
Nothing in the Hub could do it, so the most gating falsifier in the
strategy could not be run on the surface paying customers are actually on.

An operator starts one per org:

```bash
python -m hub.manage start-experiment <org_id> 0.2   # withhold 20%
python -m hub.manage experiment <org_id>             # what it established
python -m hub.manage stop-experiment <org_id>        # observations are kept
```

The customer drives the loop over MCP, or from a shell with
`commontrace prove assign` / `prove record` / `prove outcomes` — so the
experiment can be exercised once by a human, or wired into a fleet from a
script, without hand-writing MCP calls.

**The rate is a real trade, not a knob.** Withholding memory from a
fraction of occasions means those occasions get a worse product on
purpose. That is the price of knowing whether the product works at all,
it is bounded by that number, and it should be a decision someone makes
rather than a default nobody chose — which is why no migration ever turns
it on and `holdout_rate` defaults to 0.

Four properties do the real work, and each exists because its absence
fails *silently* rather than loudly:

- **Assignment is a deterministic hash** of (salt, trace, occasion), so a
  client that times out and retries gets the same arms. An occasion that
  moved between arms would not raise; it would quietly contaminate the
  comparison.
- **The salt is per-org and never edited.** Restarting starts a *new*
  experiment, and the analysis is scoped to the current salt. Pooling two
  randomizations compares two mixtures and biases every effect toward
  zero — which looks like a null result, not like a bug.
- **Eligibility is the row's existence**, not a flag on it.
  `commontrace/experiment.py` calls `eligible` "the crucial field and the
  easiest thing to get wrong"; here a row only exists because the Hub was
  asked to decide, so there is one fewer thing a client can misreport.
- **Unresolved observations are excluded, never counted as failures.** An
  agent that crashed before reporting is missing data; scoring it as a
  loss would penalise whichever arm crashed more.

`HURTS` is a first-class verdict, and it is the one correlational scoring
structurally cannot produce: a lesson that is retrieved often *because*
it fires on hard tasks looks good by retrieval count and bad by outcome,
and only the holdout can tell those apart.

### Fleet outcomes: the number the moat argument depends on

`Trace.outcome` has carried the five business-outcome fields since the
schema was written — `resolved`, `escalated`, `repeated_error`,
`frustration_signal`, token/call cost — plus `baseline`, a flag marking
traces captured *before* lessons were being injected. Every
`contribute_trace` writes all of it. Until `hub/outcomes.py`, the Hub read
that column in exactly two places (copying it onto the wire projection,
carrying it forward on amend) and computed nothing.

That was not a missing report. `STRATEGY.md` §11.3 names measured effect
on the customer's own data as the whole moat — "nobody rips out the thing
with a measured effect size on their own data" — and §11.5 names measured
resolution-rate improvement as the only pricing denominator this product
can defend. Both were claims about a number the service could not compute.
A customer who thought to run `commontrace impact` got a local version
against files on their own disk; the operator had nothing.

`fleet_outcomes` (MCP tool, org-scoped, unmetered) and
`python -m hub.manage outcomes [org_id]` (operator, all fleets) compute it.

**What it is not, stated before anything else.** This is a before/after
comparison, **not a causal estimate**. `baseline` marks a time window, so
a model upgrade, a shift in task mix, or a team simply getting better is
confounded with this product's contribution and cannot be separated from
it by any amount of statistics applied to two buckets.
`commontrace/experiment.py` is the design that *can* support a causal
claim — it withholds lessons at random, so the arms differ only by the
treatment. This module borrows that module's statistics and deliberately
not its language; `OBSERVATIONAL_CAVEAT` rides on every response and every
rendering.

Three properties keep the number quotable, and all three make the
conclusion weaker:

- **Benjamini-Hochberg across the four metrics.** Testing four things at
  α=0.05 and quoting whichever came back significant is how a null result
  becomes a win. One consequence is visible in the output and worth
  knowing: a row can show a 95% CI excluding zero and still read `no
  change`, because the interval is uncorrected and describes one metric
  while significance is judged across all four. The row says so rather
  than leaving a reader to conclude one of the numbers is broken.
- **A minimum detectable effect on every inconclusive row.** "No
  significant improvement" from 60 traces and from 60,000 are the same
  string and opposite facts.
- **`worsened` is a first-class verdict**, reported with the same
  prominence as a win and never sorted below one. A measurement instrument
  that can only return good news is not a measurement instrument, and the
  moat argument depends on this being a number a customer can trust
  against the operator's interest.

The statistics are *imported* from `commontrace/experiment.py`, not
reimplemented — the same reasoning `hub/commons.py` gives for importing
the client's MinHash. Here the specific failure a near-copy would cause is
worse than wrong, it is *disagreement*: the customer's own tooling and the
operator's report producing different deltas for the same fleet finishes
the number as evidence regardless of which was right.

For the operator, `manage.py outcomes` with no org is the closest thing
this system has to a **churn dashboard**, and a better one than
`usage`/`revenue`: those report consumption, a lagging indicator that
looks healthy right up to the renewal a customer declines. This reports
whether the thing they pay for is moving their numbers. It does not
correct across orgs, and says so — scanning fifty customers and quoting
the three that came back significant is a further multiple-comparisons
problem no per-report correction can fix.

### Entry standing: how a curated corpus stays true

Seeding and submissions both answer "how does content get in". Neither
answers "what happens when it stops being right", and a curated corpus
that only grows is one that decays — the entries stay, the world moves,
and the Knowledge Base keeps confidently serving answers that used to
work. The corpus was already collecting the signal needed to catch that
and then discarding it: `Trace.trust` is computed from every vote cast on
an entry and was read by nothing but a tie-break, and `Vote.feedback_tag`
has carried `outdated`/`wrong`/`security_concern` since it was introduced
and was consulted nowhere at all.

`hub/commons.py:entry_standing` turns those into one label, computed in
one place and carried on every Knowledge Base projection:

| Standing | Meaning |
|---|---|
| `disputed` | At least `MIN_VOTES_FOR_STANDING` (3) fleets have voted and a majority reported it did not work. |
| `stale` | The entry declared a freshness horizon at authoring time (the seed file's `review_after`) and it has passed. |
| `established` | Corroborated by enough fleets to be more than the operator's own confidence. |
| `unproven` | In the corpus, not yet judged. Where every new entry starts. |

**The design constraint that shapes every threshold: votes inform, the
operator decides.** Nothing here removes an entry on its own, at any vote
count. The strongest automatic consequences are that a disputed entry
stops counting toward the coverage figure `commons_overlap` produces and
sorts last among `commons_search` candidates — both of which make this
product's own claims *smaller*, never larger. A corpus where three
downvotes silently delete the operator's content is a corpus a competitor
can edit; `hub/tests/test_kb_standing.py:TestVotesNeverRetract` pins that
as a property rather than an intention.

The two query surfaces diverge here, for the same reason they diverge on
thresholding. `commons_overlap` produces a number customers quote, so a
contested entry is excluded from it — a wrong answer is not a solved
failure — and returned separately under `disputed_matches`, because "the
Knowledge Base has something about this and it is contested" is a
materially different answer from "the Knowledge Base has nothing".
`commons_search` is lookup, so the same entry is still returned, ranked
last and labelled: a contested answer plus the warning beats no answer.
Note that a disputed entry keeps accruing `commons_hits` — how much
traffic a bad answer is misdirecting is precisely what makes it urgent.

**Why operator curation scales, which is the actual point.** The obvious
objection to a corpus one party maintains is that review costs
O(entries), so the model dies past a few thousand. It dies only if
*finding* the bad entries is the expensive part, and it is not: every
query credits `commons_hits`, every fleet that tries an answer can vote
on it, and `feedback_tag` says what kind of wrong it was.
`hub/manage.py kb-review` turns that exhaust into a work list — security
flags first, then disputed, then past-review-date, then never-matched,
each bucket ordered by traffic affected — so review cost tracks the
**error rate** rather than the corpus size. That is the mechanism that
lets Stack Overflow and Wikipedia stay usable at a scale no editorial
staff could read: readers find the errors, editors adjudicate them.

A single vote tagged `security_concern` puts an entry at the top of that
queue regardless of the rest of the tally — the one place the rule above
is applied at n=1. The asymmetry is deliberate: reading one spurious
report costs a minute, and missing a real one means bad security advice
served from a corpus customers were told to trust. It still does not
retract, hide, or de-rank anything by itself.

`kb-retract <trace_id> [reason]` withdraws an entry — invisible to
`commons_overlap`, `commons_search`, and `vote_trace` from that moment,
via the single `hub/crud.py:commons_visible()` filter all three share.
It is deliberately **not** a delete: the row, its votes, and its hit
history survive, because "how many fleets did we serve this to before we
pulled it, and what did they say" is answerable only from exactly the
data a `DELETE` would destroy. `kb-restore` undoes it. For actually
removing content, `purge-trace` is still the path (see
`DATA_RETENTION.md` §3).

### Self-service deletion: one call for a trace, two for an organization

`delete_trace` is immediate, org-scoped, and irreversible -- and that is
the right trust level for it. A compromised API key can already overwrite
a trace's real content via `amend_trace`; letting it also delete one trace
at a time is not a categorically new risk, so there is no reason to gate
it behind anything beyond ordinary auth.

Whole-organization deletion is a different risk shape: one call, and
every trace, vote, api key, and Knowledge Base submission this org has is
gone, unrecoverably, in the time it takes the request to round-trip. A
single compromised key executing that with no confirmation step was an
open authorization question in an earlier pass of this document. The
answer implemented is a two-call design:

- `request_account_deletion()` deletes nothing. It returns a one-time
  confirmation token, plus the earliest time it may be used
  (`confirm_not_before`) and when it expires.
- `confirm_account_deletion(confirmation_token)` needs the exact token AND
  `crud.DELETION_GRACE_SECONDS` (5 minutes) to have actually elapsed since
  the request -- checked by comparing timestamps at confirm time, so this
  needs no background scheduler or task queue, just two columns on
  `organizations` (`hub/models.py`).
- `cancel_account_deletion()` needs no token -- cancelling is a safety
  action, not a destructive one, so any of the org's own valid keys may
  call it at any point before confirmation.

Five minutes is not a long delay, and it is not meant to stop a
sophisticated attacker who holds the key for that whole window. It is
meant to make the *common* failure modes non-catastrophic: a client bug
that calls the wrong tool, a copy-pasted curl command run against the
wrong org, a key an operator is already in the process of revoking when
the request comes in. `request_account_deletion` is audit-logged like
every other consequential action (`hub/audit.py`), so an operator alerting
on that log has the whole grace window to notice and `revoke-key` a
credential they don't recognize before `confirm_account_deletion` can
possibly succeed.

If this org has ever used self-serve billing (`hub/billing.py`) and has a
live Stripe subscription, `confirm_account_deletion` cancels it FIRST,
before deleting anything -- deleting the org row out from under an active
subscription would leave it charging that customer's card every billing
cycle with no CommonTrace account left to ever notice. If Stripe cannot
be reached to cancel it, nothing is deleted: the call fails with
`deletion_blocked` instead, so a client knows to retry rather than treat
the deletion as done. `python -m hub.manage purge-org` does the same
cancel-first check at the operator-CLI trust level.

### Why there's no `lessons` table

`Trace` and `Lesson` are both loaded by `hub/schema_validation.py` (per the
brief: load both schema files from disk, don't hand-write validation logic),
but only `Trace` has a table in `hub/models.py`. None of the six Hub tools
accept or return a Lesson-shaped object — per `PROTOCOL.md` §4, "Lessons are
local-store scaffolding ... the Curator/Validator roles turn Traces into
Lessons before promoting the durable, reusable half of a Lesson to a Hub
Trace via `contribute_trace`." A `lessons` table would be dead schema. If a
future Hub tool ever needs to accept a Lesson, `validate_lesson()` is already
there and ready.

### Auth follow-ups (not implemented)

API-key-per-org (argon2-hashed, shown once, rotatable via
`hub/manage.py rotate-key`) is the whole auth story today, per the brief's
explicit MVP scope. Not implemented, and worth doing before this serves
traffic beyond a pilot:

- **OAuth/JWT.** The `mcp` SDK's built-in auth framework
  (`mcp.server.auth`) is OAuth-resource-server-shaped (issuer URLs, token
  introspection) and deliberately unused here — see `hub/auth.py`'s module
  docstring for why a small Starlette middleware was simpler and more
  honest about what's actually implemented than forcing API keys through
  an OAuth-shaped surface that isn't OAuth.
- **Per-key scopes.** Every key currently has full read/write access to its
  org's traces. Read-only keys, or per-tool scoping, aren't implemented.

### Abuse controls (implemented, with a known scaling limit)

`hub/abuse.py`'s rate limiter is an in-memory token bucket keyed by
`org_id`. That's fine for a single server process and is what the brief's
"start conservative" MVP scope calls for, but it resets on restart and does
not coordinate across multiple instances. A horizontally-scaled deployment
needs a shared store (Redis `INCR`+`EXPIRE`, or a Postgres-backed bucket
table) — not implemented, to avoid pulling in a Redis dependency for an MVP
that's meant to run as one process.

Three properties worth knowing, because each one was a real defect:

- **Every refusal carries `Retry-After`** (the HTTP 429s, and the
  `retry_after` field on a tool-level `rate_limited` error). Without it a
  refused client can only guess, and a client guessing short against a
  limiter already saying no turns one burst into a sustained stampede.
  `commontrace sync` paces its whole batch off this value.
- **The auth-attempt limiter charges only credentials that fail to
  verify.** Most requests resolve via an indexed `key_hmac` lookup in
  ~1ms now; this limiter's job is bounding the Argon2 CPU an
  unauthenticated source can still force through the legacy fallback path
  (an unmigrated or guessed-prefix key). Charging successful
  authentications too made it throttle the legitimate heavy client
  hardest — a bulk push is hundreds of successful authentications from
  one address against a 60/min budget. Valid callers
  are governed by the per-org read limiter instead, where they are
  authenticated, accountable and metered.
- **Tracked keys are capped** (`_MAX_TRACKED_KEYS`). The idle sweep alone
  evicts nothing for an hour, and the client-address-keyed limiters are
  keyed on something the peer chooses — any address out of an IPv6 /64 —
  so an unauthenticated flood could otherwise grow process memory without
  bound via the limiter meant to prevent exactly that.

The spam heuristic (`suspicion_reason` in `hub/abuse.py`) is intentionally
simple — too many URLs, or near-zero character diversity — and is explicitly
documented in its own docstring as a placeholder, not a moderation system.
Replace/extend it as real abuse patterns are observed; do not read its
current thresholds as a considered content-moderation policy.

The same function also runs `commontrace/memory_guard.py`'s content-safety
scan (OWASP ASI06: Memory & Context Poisoning) over the same three fields,
and quarantines on a HIGH-confidence secret (a structured AWS/GitHub/Slack/
Stripe/Google/Anthropic token shape, a PEM private key block, a JWT) or a
prompt-injection pattern (instruction-override phrasing, a forged
system-role block, hidden zero-width/bidi-override Unicode). PII findings
(email, phone, a Luhn-valid card number) are surfaced by that module but
never quarantine anything on their own — a support trace legitimately
mentions a customer's email. This is pattern matching, not semantic
understanding, and carries the same "not exhaustive" caveat as the spam
heuristic; what it changes is the default, from nothing being checked to a
known-dangerous shape being caught before it reaches `search_traces` (and,
on the client side, before a lesson containing one can be activated —
`commontrace lesson approve` / the MCP `approve_lesson` tool run the same
scan).

### `related` / `CO_RETRIEVED` — partially implemented

`Trace.related` (in `trace.schema.json`) documents three relationship kinds:
`SUPERSEDES`, `AMENDS`, `CO_RETRIEVED`. `amend_trace` populates `AMENDS`
(from the new trace) and `SUPERSEDED_BY` (on the original) via
`hub/models.py`'s `TraceRelation` table. `CO_RETRIEVED` (traces that tend to
surface together in the same `search_traces` call) is not computed — it
needs session-level co-retrieval tracking that wasn't in scope for this MVP.
`trust` (in `_to_wire`/`hub/crud.py:vote_trace`) is a simple
`up / (up + down)` ratio with a neutral `0.5` prior when there are no votes
yet; treat it as a starting point, not a calibrated reputation model. It is
no longer read only as a tie-break — "Entry standing" above is what
consumes it — but the standing thresholds are deliberately coarse for
exactly this reason: a ratio over three votes does not support a finer
judgement than "a majority said it failed", and a model that pretended
otherwise would be false precision on top of an uncalibrated number.

### Container image: built and smoke-tested in CI

`Dockerfile` and `docker-compose.yml` exist at the repo root (an earlier
revision of this file said they deliberately did not — that is no longer
true). They were authored in an environment with no Docker daemon, so CI's
`docker-build` job is what actually proves them: it builds the image, starts
the container, and asserts `/healthz` serves while `/readyz` returns 503
with no database reachable — which also confirms the liveness/readiness
split behaves correctly in a real container, not just in unit tests.

Still unproven, so worth saying: **the compose stack is not exercised by
CI** (only the image is), and neither has been run against a
production-like environment. Do a rehearsal deploy first.

## Operator CLI (`hub/manage.py`)

There is no web admin panel — this CLI *is* the admin/monitoring surface,
consistent with the rest of the Hub being a from-a-checkout service you
operate, not a hosted product with its own UI. A web dashboard is a
reasonable future addition, but it needs its own cross-org admin auth model
(a superadmin credential, distinct from the org-scoped API keys `hub/auth.py`
issues) — a bigger, separate decision this pass didn't make.

```
python -m hub.manage create-org <name>
python -m hub.manage issue-key <org_id>
python -m hub.manage rotate-key <key_id>
python -m hub.manage revoke-key <key_id>
python -m hub.manage list-orgs

python -m hub.manage stats                       # orgs, active keys, traces, quarantined, votes, mean trust
python -m hub.manage outcomes [org_id]            # is the product working, per fleet? (observational)
python -m hub.manage start-experiment <org_id> [rate]   # begin a randomized holdout (causal)
python -m hub.manage experiment <org_id>          # what the holdout established, per trace
python -m hub.manage stop-experiment <org_id>     # stop withholding; observations are kept
python -m hub.manage value <org_id> [value_per_occasion]  # what the memory was worth, causally --
                                                   # occasions improved, priced only if you pass a
                                                   # rate (never stored); the CLI path to the same
                                                   # numbers a customer sees on their own Proof page
python -m hub.manage kb-stats                     # corpus size, hits delivered, standing breakdown, submission funnel
python -m hub.manage kb-review [limit]            # which entries need a human, worst first
python -m hub.manage kb-retract <trace_id> [reason]  # withdraw an entry from the Knowledge Base (reversible)
python -m hub.manage kb-restore <trace_id>        # put a retracted entry back
python -m hub.manage list-quarantined [org_id]    # the abuse-control review queue (hub/abuse.py)
python -m hub.manage release-quarantine <trace_id>  # reviewed, it's fine -> becomes search_traces-eligible
python -m hub.manage purge-trace <trace_id>       # permanent delete, irreversible
python -m hub.manage purge-org <org_id>           # permanent delete (cascades), irreversible
```

The raw API key is only ever printed at issuance/rotation time — it is
hashed with argon2 before the row is written and never logged or returned by
any tool/endpoint afterward. There is no "show me the key again" path by
design; rotate if it's lost.

`kb-retract` is un-publishing, not deleting: the entry stops being served
but its row, votes, and hit history survive, and `kb-restore` reverses it.
It takes no confirmation prompt precisely because it is reversible —
putting a prompt in front of a reversible action trains operators to type
`y` without reading, which is what makes the prompt in front of the
irreversible one worthless. `purge-trace` is the path when the content
must actually be gone.

`purge-trace`/`purge-org` are the operator/DB-access-trust-level data-
deletion path — for when an org has lost its own API keys, or an operator
needs to act without one. An org's own key can now do the equivalent
itself, at MCP-tool trust level: `delete_trace` for one trace (immediate,
same risk profile as any other write a key can already make via
`amend_trace`), and `request_account_deletion` / `confirm_account_deletion`
/ `cancel_account_deletion` for the whole organization. Whole-account
deletion answers the authorization question an earlier pass of this
document left open — *should a single compromised key be able to wipe an
org's entire trace history with no confirmation step?* — with no:
`request_account_deletion` deletes nothing by itself, only
`confirm_account_deletion` does, and it refuses to run until a mandatory
delay (`crud.DELETION_GRACE_SECONDS`, 5 minutes) has passed since the
request — long enough for an operator watching the audit log (every
request is recorded there) to `revoke-key` a compromised credential
first. See hub/crud.py:request_org_deletion and
`hub/tests/test_self_service_deletion.py`.
