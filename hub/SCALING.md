# Does serving one customer get more expensive as they succeed?

`STRATEGY.md` §13.2 lists five links the (A) case depends on. Link 3 is
the only one marked **"unmeasured, and the weakest link nobody has looked
at"**, and its falsifier is stated precisely:

> If serving cost grows with corpus size faster than value does, this is a
> services business wearing infrastructure clothes.

That sentence is the difference between an infrastructure multiple and a
services multiple. This document is the measured answer to its cost half.
Reproduce with `python -m hub.bench_scaling`.

## Result

Corpus sizes 1,000 → 64,000 traces in one org (a 64× range), median of 9
runs per point, PostgreSQL 16. `alpha` is the fitted exponent in
`latency ~ size**alpha`; the fit is an ordinary least-squares slope on
log-log axes, so it is a straight-line regression rather than a curve fit
that could go wrong quietly.

| read path | 1,000 | 4,000 | 16,000 | 64,000 | growth | alpha | |
|---|---:|---:|---:|---:|---:|---:|---|
| `search_traces` (selective) | 11.00 ms | 19.30 | 58.03 | 59.35 | 5.4× | **0.44** | sublinear |
| `search_traces` (natural lang) | 21.67 | 46.26 | 137.74 | 65.89 | 3.0× | **0.32** | sublinear |
| `search_traces` (all-common terms) | 11.43 | 18.63 | 15.41 | 17.88 | 1.6× | **0.08** | flat |
| `search_traces` (by tag) | 5.11 | 4.95 | 4.84 | 5.23 | 1.0× | **0.00** | flat |
| `list_tags` | 1.71 | 4.17 | 16.25 | 32.65 | 19.1× | 0.74 | sublinear |
| `entitlements` | 4.08 | 5.61 | 13.97 | 47.36 | 11.6× | 0.60 | sublinear |
| `agents_under_management` | 1.71 | 3.03 | 9.25 | 33.12 | 19.4× | 0.72 | sublinear |
| `fleet_outcomes` | 6.76 | 20.75 | 40.44 | 96.45 | 14.3× | **0.62** | sublinear |
| `causal_effects` | 15.15 | 53.72 | 294.99 | 1440.40 | 95.0× | **1.11** | LINEAR-OR-WORSE |
| `value_delivered` | 14.34 | 51.73 | 311.60 | 1399.29 | 97.6× | **1.12** | LINEAR-OR-WORSE |

**No path against a customer's TRACE corpus grows linearly.** A 64×
increase in a customer's accumulated history costs 3.0× on the read the
product exists for (an agent describing its task in a sentence) and at
worst ~20× on the operator-facing reports. On the cost side, link 3's
falsifier does not fire for that axis.

**The last two rows measure a different axis, and it does not clear the
same bar.** `causal_effects`/`value_delivered` scale with the org's
`HoldoutObservation` count under its *current* experiment (§ below), not
its trace count -- the sweep grows both in lockstep at the same labelled
sizes (`hub/bench_scaling.py:_seed_holdout`), which is why they share this
table rather than needing a second one, but the column headers name two
different corpora for these two rows. Read on for what that means and
what was and was not done about it.

The three `search_traces` rows are three query shapes, and the first two
now carry a `search_traces` cost the earlier runs of this document did not
have: a document-frequency probe on every text search
(`hub/search.py:term_frequency_stmt`), which is why *selective* moved from
0.19/11.67 ms to 0.44/59.35 ms. That is the price of the third row and of
the natural-language row existing at all — see **The term budget**, below.

The absolute milliseconds are this machine's and transfer to nothing. Only
the exponents transfer.

## What this does and does not settle

It settles the **cost** half. §13.2's falsifier compares cost growth
against *value* growth, and value per query needs real customers rather
than synthetic rows — that is what `fleet_outcomes` and `commons_hits`
are for. So a clean result here does not prove link 3; it removes the
cost-side objection to it, which is the half answerable without a
customer.

It also says nothing about **concurrency**. Every number above is a single
query against an otherwise idle database. That measurement is now made
separately, by `hub/bench_concurrency.py` — see "Concurrency: measured"
below.

## Concurrency: measured

Reproduce: `python -m hub.bench_concurrency --clients 1,8,32,128 --backend both`

N simultaneous authenticated clients through the real
`ApiKeyAuthMiddleware`, against an empty handler — so what is measured is
the per-request *floor* (key verification plus two rate-limiter
decisions), not query cost, which is what the rest of this document
covers. 400 requests per level, one Hub process, co-located Postgres:

| backend | clients | rps | p50 | p95 | p99 |
|---|---|---|---|---|---|
| memory | 1 | 392 | 2.5 ms | 3.1 ms | 3.6 ms |
| memory | 8 | 605 | 12.1 ms | 18.5 ms | 37.9 ms |
| memory | 32 | 484 | 56.9 ms | 134.8 ms | 157.0 ms |
| memory | 128 | 480 | 223.7 ms | 363.2 ms | 511.7 ms |
| postgres | 1 | 142 | 6.9 ms | 8.4 ms | 9.7 ms |
| postgres | 8 | 191 | 40.5 ms | 51.2 ms | 84.5 ms |
| postgres | 32 | 161 | 186.4 ms | 307.5 ms | 352.4 ms |
| postgres | 128 | 165 | 692.3 ms | 854.4 ms | 1375.7 ms |

**Neither backend scales with concurrency.** 128× the clients buys 1.23×
the throughput on memory and 1.16× on postgres; past ~8 clients, added
load becomes queue and shows up entirely in the tail. For a single
process that is partly physics — one event loop is one core for Python
bytecode — and the practical answer is more replicas. Two things here are
not physics, and both are worth naming:

**The multi-replica configuration is the slow one.** `postgres` is the
documented choice for any deployment with more than one replica
(`hub/DEPLOYMENT.md` §6), and it serves **2.9× less throughput** with a
**2.7× worse p99** than the single-process default. The mechanism is
already conceded in `hub/abuse.py:PostgresRateLimiter`'s own docstring:
its `check()` is a sync method called un-awaited from async code, so it
blocks the ASGI event loop on `run_coroutine_threadsafe(...).result()`
twice per authenticated request. Every other request on that replica
waits. This is the shape the benchmark exists to catch, and it is a fixed
cost paid before any real work begins.

**p99 at 128 clients is 512 ms (memory) / 1.38 s (postgres) for an empty
handler.** Any real SLO has to be stated on top of that floor, not
independently of it.

These are single-process numbers on a developer machine, so read them as
a shape and a ratio rather than a capacity plan.

### What the blocking limiter actually cost — two wrong guesses, then the answer

The obvious reading of the table above is that the blocking `.result()`
call is what makes `postgres` slow, so awaiting it should recover the
gap. `hub/abuse.py:_await_on_pool_loop` now awaits it. **It did not
recover the gap**, and the number is recorded here rather than quietly
dropped:

| | rps @128 | p99 @128 |
|---|---|---|
| blocking `.result()` | 165 | 1376 ms |
| awaited | 177 | 1333 ms |

That is noise. The second guess — the connection pool — is also wrong:
raising `_SharedPgPool._POOL_MAX_SIZE` from 5 to 32 moved throughput from
177 to 170 rps, i.e. nothing. Neither waiting nor connections is the
ceiling.

The reason is structural, and it is a property of the benchmark as much
as of the code: when *every* request needs a limiter decision, freeing
the event loop cannot help, because everything it could switch to is
queued behind the same work. Throughput here is bounded by per-request
CPU on one process — including the cross-thread handoff to the pool's
loop, which is paid whether or not the caller blocks.

**The defect was real, but it was never a throughput defect.** It shows
up in work that does *not* need a limiter decision. An unauthenticated
`/healthz`, polled while 32 clients hammer the limited path:

| | p50 | p95 | p99 | probes completed |
|---|---|---|---|---|
| blocking | 42.3 ms | 58.6 ms | 62.8 ms | 81 |
| awaited | **2.3 ms** | **5.3 ms** | **8.6 ms** | **448** |

A replica whose health endpoint answers in 63 ms because something else
is rate-limited looks unhealthy to a load balancer for reasons that have
nothing to do with its health, and gets pulled from rotation under
exactly the load where it is needed. That is what awaiting the future
fixes — not the RPS column.

**Where the throughput ceiling actually is** remains open. The candidate
the evidence now points at is per-request CPU on a single event loop, of
which the `postgres` backend's two cross-thread round trips per request
are the largest addition. Moving the pool onto the serving loop (no
handoff at all) is the change that would test it. Not attempted here,
and not claimed.

## Two findings from the first run, both of which changed the answer

The first run reported **two** paths as linear-or-worse. Both turned out
to be worth the trouble, for opposite reasons, and the instrument had to
be corrected before the number meant anything — the same discipline
`commons/eval/RESULTS.md` applies to recall.

**1. `fleet_outcomes` was genuinely superlinear (alpha 1.12), and it was
new.** It selected every matching trace's `outcome` column and counted in
Python — tens of thousands of JSONB blobs crossing the wire, one Python
dict built per row, to produce six integers. Moving the counting into a
single grouped aggregate (`hub/crud.py:fleet_outcomes`) took 64,000
traces from **572 ms to 95 ms** and the exponent from **1.12 to 0.62**.

The scan is still proportional to the org's history, and that is not a
bug to fix later: a question about all of history cannot be answered
without reading all of it. What was removed was the per-row transfer, not
the scan. If a deployment ever holds a fleet where 0.62 stops being
acceptable, the next step is a materialized rollup keyed by
`(org_id, baseline)` refreshed on write — deliberately not built now,
because it trades correctness-by-construction for a cache that can go
stale, and nothing yet needs it.

**2. `search_traces` was an artifact of the benchmark, not the product.**
The generator gave every synthetic row near-identical title text, so the
probe query matched **64,000 of 64,000 rows**. `EXPLAIN` showed a parallel
sequential scan feeding a top-N heapsort — which is correct behaviour for
that query, because `ORDER BY ts_rank(...)` must score every match and no
index can serve it. With realistic text diversity the same path measures
**0.19**.

Both numbers are reported above rather than only the flattering one,
because the worst case is real: a deliberately broad query does cost
`O(matches)`, and a fleet that searches for common words will find it.
`hub/tests/test_bench_scaling.py:TestGeneratedCorpusIsSelective` pins the
generator's diversity so the artifact cannot quietly return.

## `causal_effects`/`value_delivered`: genuinely linear, and not an artifact this time

These two rows measure a different corpus than every other row in the
table above: `HoldoutObservation` count under an org's *current*
experiment (bounded by `Organization.holdout_salt`, not by history --
`--configure`/`start-experiment` rotating the rate starts this count over
at zero), not trace count. They were absent from every earlier run of this
document for exactly the reason `search_traces` almost was measured
wrong: a probe added to the existing sweep, which only grows *traces*,
would have measured a flat line regardless of how the path actually
scales, for having nothing to do with the axis being varied.
`hub/bench_scaling.py:_seed_holdout` grows both corpora in lockstep so one
sweep answers both questions.

**The exponent is a genuine ~1.1, not a benchmark artifact, and it is not
being reported as fixed.** Profiling the 200,000-observation case (a
synthetic run, not this table, at 20 lessons rather than 80) found the
single largest cost was `sqlalchemy.orm.loading` -- `select
(HoldoutObservation)` hydrates a full mapped ORM entity, with its identity
map and instrumented attributes, for every row, when the function reads
only 7 of the model's 9 columns. That accounted for roughly 60% of a
12-second call. Replacing it with a Core column-select of exactly the
fields used -- `trace_id, occasion_id, injected, succeeded, salt,
created_at, trace_revision` -- returns lightweight `Row` tuples instead,
with **no change to what is computed**: same rows, same fields, same
downstream `Assignment`/`HoldoutObservation` objects, all 25 existing
tests in `hub/tests/test_holdout.py`/`test_fleet_outcomes.py` and this
codebase's own broader suite passing unchanged. Measured on that same
200k-row run: **9.7s → 6.2s, a real 36% reduction** -- and the fitted
exponent over the table above (already reflecting this fix) is still
**1.11**, because the fix reduced a constant factor, not the shape.

That shape is not a bug to chase away. `experiment.analyze` needs the
per-arm success/failure counts for every lesson, correctly computed --
reducible to aggregates in principle. `integrity.audit`'s five checks
(differential attrition, arm balance, mid-run drift, conflicting arms,
treatment stability) are what make this product's causal claim trustworthy
rather than merely computed, and each one is inherently row-level: arm
balance and attrition need per-observation timestamps for the power
projection, treatment stability needs each observation's own recorded
revision, and conflicting-arm detection is specifically "does the same
(lesson, occasion) pair appear in both arms" -- a question aggregate counts
cannot answer at all. Reading every row at least once is the correctness
floor for auditing a randomized comparison honestly, not an oversight
`fleet_outcomes`'s grouped-aggregate fix happened to miss.

Rewriting that audit machinery to move some of it into SQL, or to sample
rather than read every row, would trade away the exact property
`hub/crud.py:causal_effects`'s own docstring and this codebase's own
CHANGELOG spent several rounds establishing: that a compromised experiment
is *detected*, not silently averaged over. That is a larger, riskier
change to the single most safety-critical statistics in this product than
this pass is prepared to make unmeasured and unreviewed. What is true in
the meantime, and worth an operator's attention if `HoldoutObservation`
volume for one org's one experiment ever approaches six figures: **an
experiment is bounded by design, not by accident** -- `PILOT.md`'s whole
argument is to size and plan a pilot's duration up front rather than run
one indefinitely, and reconfiguring the rate (which every real reason to
change it already calls for) resets this count to zero. The practical
exposure is "how long was this experiment left running against real
volume before anyone looked at the result," which the existing power
projection (`integrity.project`) already answers.

## The term budget, and the finding that made it necessary

The second finding below closed by saying the worst case is real: *"a
deliberately broad query does cost `O(matches)`, and a fleet that searches
for common words will find it."* That sentence was written about a
conjunctive matcher, where a broad query was an unusual event. Once
`hub/search.py` relaxed the operator so that natural-language retrieval
works at all (`hub/RETRIEVAL.md`), a broad query stopped being unusual:
**every** sentence-length query is broad, because OR-ing ten words matches
anything containing any one of them.

Measured immediately after that change, on this same 64,000-trace corpus:
one query term present in every trace (`retri`) produced the entire
64,000-row match set on its own — the other nine lexemes matched nothing at
all — and `ts_rank` over those rows was 188 ms of a 233 ms query. The fitted
exponent was **0.87, linear-or-worse**: §13.2's failure mode, introduced by
a fix for something else.

That term also could not rank anything, since it scores every document
identically. So `hub/search.py:choose_terms` drops any term matching more
than `RANK_BUDGET` (8,000) documents and takes the rest rarest first while
their frequencies still sum to the budget — a union is never larger than the
sum, so the ranker provably never scores more than `RANK_BUDGET` rows
however large the corpus grows.

| `search_traces` (natural language), 64,000 traces | before | after |
|---|---:|---:|
| latency | 754 ms | **66 ms** |
| fitted alpha | **0.87** linear-or-worse | **0.32** sublinear |

The **all-common terms** row measures what the bound does at its extreme:
every lexeme in that query is above the budget, so nothing is left to match
on, the search is not run, and the row measures the cost of establishing
that (0.08, flat). It is the successor to the old *matches all* row, which
measured the same query when it still returned 64,000 scored rows.

The cost this adds is the probe, and it is paid on every text search
including short keyword ones — the *selective* row's 0.19 → 0.44. Bounded,
though: the probe caps each term's count at the budget, so its cost stops
growing with the corpus while an unbounded ranking scan would not have.

## Why the Knowledge Base paths are absent

`commons_overlap` and `commons_search` scan the **operator's** curated
corpus, which does not grow when a customer succeeds. They are not part of
this question, and they carry their own explicit bound
(`commons.max_corpus_scan()`, with `corpus_truncated` reported when it
bites).
