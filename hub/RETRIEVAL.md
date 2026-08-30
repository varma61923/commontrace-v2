# Does the Hub find the right memory when the task is described in the operator's own words?

`STRATEGY.md` §13.2 calls this **link 2** and marks it *"measured, holds"*,
citing 84.8% recall@1 / 95.7%@5 / 97.8% findable. §13.3 says link 2 is one
half of the only thing that makes this more than a good DevTools business.

Those numbers were real. They measured
`commontrace/retrieval.py:rank_lessons` — the **local, file-based tier**
(`commons/eval/retrieval_tiers.py`). Nothing had ever measured
`hub/crud.py:search_traces`, which is the only retrieval path a Hub
customer has, and the two tiers combined query terms with **opposite
boolean operators**.

Reproduce with `python -m hub.bench_retrieval --probes all`.

## Result

Same 46-record substrate corpus, same held-out paraphrased probes, same
queries, both tiers. `probes-v2` was written after the corpus was frozen
and nothing has been changed on the strength of it.

| | Hub, before | Hub, after | local tier |
|---|---:|---:|---:|
| recall@1 (probes-v1) | **0.0%** | 95.7% | 84.8% |
| recall@1 (probes-v2, held out) | **0.0%** | 95.7% | 87.0% |
| recall@5 (v1 / v2) | 0.0% / 0.0% | 97.8% / 100% | 95.7% / 93.5% |
| findable (v1 / v2) | 0.0% / 0.0% | 97.8% / 100% | 97.8% / 97.8% |
| MRR (v1 / v2) | 0.000 / 0.000 | 0.967 / 0.973 | 0.887 / 0.897 |
| **returned nothing at all** | **100%** | **0%** | 0% |

With 5,000 unrelated traces seeded alongside the corpus
(`--distractors 5000`), recall@1 holds at 91.3% / 95.7% — ranking, not the
small corpus, is what puts the answer first.

## What was wrong

`plainto_tsquery` **ANDs** every lexeme it extracts:

```
plainto_tsquery('english', 'customer charged twice for one order')
  -> 'custom' & 'charg' & 'twice' & 'one' & 'order'
```

A trace had to contain *all* of them. For the query shape the product
exists to serve — an agent describing, in its own words, the task it is
about to attempt — that conjunction is essentially never satisfied. The
local tier does the opposite: weighted token overlap, ranked, top-k, no
threshold, which is why §12.7 could say "any single shared content word
puts a lesson on the list."

**The failure was silent in every direction.** An unmatched search returns
`{"traces": []}` with HTTP 200; the agent correctly concludes there is no
relevant prior experience and proceeds; the customer sees a product that
"has no memory of that yet", which is indistinguishable from an empty
corpus. And `Trace.retrievals` counts rows *returned*, so a query that
matched nothing incremented nothing and left no record of having been
asked. Nothing in the Hub's telemetry separated a broken retrieval tier
from a customer who had not stored much.

It also survived a 1,300-test suite, because nothing in that suite ever
issued a query longer than two words.

## What this would have done to the falsifier

`STRATEGY.md` §19 added a randomized holdout so a fleet can measure the
causal effect of injecting retrieved memory. §13.2 calls running it *"the
cheapest falsifier in the document and it should be run first."*

If retrieval returns nothing, both arms get nothing, the measured effect is
zero, and the honest reading of that null — *per-org memory does not
deliver measurable value*, link 1's falsifier firing — would have been
drawn about a product whose retrieval never fired. The cheapest, most
gating falsifier in the document would have returned a **false negative**,
and everything downstream of link 1 would have been abandoned on it.

## The cost of relaxing the operator, and what it required

Matching *any* lexeme means a sentence-length query matches most of a
corpus, and `ORDER BY ts_rank(...)` must score every match. Measured on a
64,000-trace org, one query term present in every trace (`retri`) produced
the entire 64,000-row match set on its own — the other nine lexemes matched
nothing — and ranking it cost 188 ms of a 233 ms query.
`hub/bench_scaling.py` fitted the path at **alpha 0.87: linear-or-worse**,
which is precisely §13.2's link-3 failure mode.

That term also could not *rank* anything: it scores every document
identically. It only drags in traces whose sole connection to the query is
one corpus-wide word — and an agent injects what it is handed, so that is
not a weak answer to skim past, it is context poisoning.

So `hub/search.py:choose_terms` decides which lexemes are matched on: a
term appearing in more than `RANK_BUDGET` (8,000) of the org's traces is
dropped as non-discriminating, and the rest are taken **rarest first** while
their frequencies still sum to the budget — rarest first because that is the
order of information content, and the sum because a union is never larger
than it, so the ranker provably never scores more than `RANK_BUDGET` rows.
Document frequencies come from one probe whose per-term count is capped at
the budget, so the probe costs ~10 ms against a 64,000-trace org and does
not grow with it.

| | before | after |
|---|---:|---:|
| `search_traces` (natural language), 64,000 traces | 754 ms | **66 ms** |
| fitted alpha over a 64× corpus range | **0.87** linear-or-worse | **0.32** sublinear |

The cost fix and the quality fix are the same decision. Deciding which
terms carry information is what makes matching on any of them safe.

## What it costs, stated plainly

**Every negative control now returns something.** The 22 failures
deliberately absent from the corpus come back with candidates at every k —
0% before, 100% after. That is not a regression, it is the trade §12.7
identifies as correct for this output contract: a ranked list a human or an
agent skims must not hide the answer, and a weak match costs a glance. The
local tier pays exactly the same price, which is why the comparison above
is like for like. A *coverage percentage* must make the opposite trade, and
`commons_overlap` still does (`commons/eval/RESULTS.md`).

**A query whose every term is too common now returns nothing** rather than
returning traces that share only that term. `terms_ignored` in the response
says which terms were dropped and why, so the caller can tell that apart
from an empty corpus.

## What this does not settle

Everything `commons/eval/run.py` caveats, unchanged: the same author wrote
the corpus and the probes, so shared conceptual framing survives paraphrase
and these are an **optimistic bound** on a real fleet, not an estimate of
one. What transfers is not the level but the **difference between two tiers
measured on identical inputs** — author bias inflates both equally, and it
cannot explain 0.0% against 84.8%.

The number that would settle it is recall on a real fleet's own corpus,
with that fleet's own queries. That needs a customer. What exists now is
the instrument to read it on one: `python -m hub.manage retrieval` reports
per-org search counts and miss rate, without storing a single query.
