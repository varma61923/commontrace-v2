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
| `search_traces` (selective) | 5.27 ms | 6.69 | 8.71 | 11.67 | 2.2× | **0.19** | sublinear |
| `search_traces` (matches all) | 6.17 | 11.57 | 25.79 | 214.36 | 34.8× | 0.83 | sublinear |
| `search_traces` (by tag) | 4.76 | 4.95 | 4.80 | 4.79 | 1.0× | **-0.00** | flat |
| `list_tags` | 1.52 | 4.01 | 14.26 | 31.20 | 20.5× | 0.74 | sublinear |
| `entitlements` | 3.90 | 5.39 | 11.34 | 44.12 | 11.3× | 0.58 | sublinear |
| `agents_under_management` | 1.68 | 2.96 | 7.51 | 28.82 | 17.1× | 0.68 | sublinear |
| `fleet_outcomes` | 6.69 | 17.16 | 38.86 | 90.33 | 13.5× | **0.62** | sublinear |

**No path grows linearly with the customer's own corpus.** A 64× increase
in a customer's accumulated history costs 2.2× on the read they issue most
(a specific failure lookup) and at worst ~20× on the operator-facing
reports. On the cost side, link 3's falsifier does not fire.

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
query against an otherwise idle database. Cost per customer at N
simultaneous customers is a different measurement and is not made here.

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

## Why the Knowledge Base paths are absent

`commons_overlap` and `commons_search` scan the **operator's** curated
corpus, which does not grow when a customer succeeds. They are not part of
this question, and they carry their own explicit bound
(`commons.max_corpus_scan()`, with `corpus_truncated` reported when it
bites).
