# Commons coverage: measured, not asserted

Reproduce: `python commons/eval/run.py`
Reproduce the first-customer scenario: `commontrace commons report --from <your export>`

| | |
|---|---|
| Corpus | `commons/seed/substrate-v1.jsonl`, 46 records |
| Probes | `commons/eval/probes-v1.jsonl`, 46 held-out positives + 22 negative controls |
| Matcher | `hub/commons.py` → `commontrace/overlap.py` MinHash, 128 permutations |
| Threshold | 0.30 — the shipped default, unmodified for this evaluation |

## Result

| Metric | Value |
|---|---|
| Recall on positives | **10.9%** (5/46) |
| Of those matches, the right record | **100%** (5/5) |
| False positives on negative controls | **0%** (0/22) |

Sensitivity, printed because one operating point hides the shape of the
trade-off — **not** as a menu to pick a better-looking number from:

| Threshold | Recall | False positive |
|---|---|---|
| 0.10 | 78.3% | 22.7% |
| 0.15 | 47.8% | 13.6% |
| 0.20 | 32.6% | 4.5% |
| 0.25 | 17.4% | 0.0% |
| **0.30 (shipped)** | **10.9%** | **0.0%** |
| 0.35 | 2.2% | 0.0% |
| 0.40 | 0.0% | 0.0% |

## What this says

**The coverage percentage `commons_overlap` returns is a floor, not an
estimate.** When a fleet describes a failure in its own words and the
commons genuinely contains that failure, the matcher finds it about one
time in nine. A prospect who runs `commons_overlap` today and sees 5%
should not conclude the commons has 5% of their problems; they should
conclude it has *at least* that.

**What it gets right, it gets right.** Every single match was to the exact
record the probe was written against, and not one of the 22 absent
failures was reported as covered — including deliberate near misses
(leap-year date arithmetic against a corpus containing DST; read-replica
staleness against a corpus full of database entries). Nothing here
over-claims. The failure mode is silence, not noise.

**No threshold fixes this.** Recall only becomes useful around 0.10, where
almost a quarter of absent failures are reported as present. That is not a
tuning problem; it is the representation. Jaccard similarity over content
words is a *lexical* measure, and two engineers describing the same
substrate failure — "connection pool exhausted during a retry storm" and
"during a spike everything starts timing out waiting to acquire a
connection" — share almost no words. The measure is doing exactly what it
says; it is the wrong measure for this question.

**So the threshold was not moved.** `DEFAULT_MATCH_THRESHOLD` in
`commontrace/overlap.py` says "tune it against labelled pairs, not by eye",
and this file is the first set of labelled pairs that has ever existed for
it. Moving it now would trade a defensible 0% false-positive rate for a
recall number that reads better in a deck, on evidence from a probe set
written by the same author as the corpus. That is the wrong trade, and it
is the specific way this kind of number gets quoted and then falls apart in
front of a customer.

## What this looks like to a first customer

The numbers above are a lab result. Here is the same defect as a sales
call, run end to end against a seeded Hub with `commons report --from`:

A prospect's incident export, ten rows, nine distinct after collapsing a
repeat. Seven of the nine are failures this corpus **provably contains** —
the redelivered payment webhook, the exhausted connection pool, the JWT
clock drift, the pod killed mid-rollout, float money, the `ADD COLUMN NOT
NULL` migration, the retry storm. Two are deliberate non-failures (a
leaking coffee machine, a broken badge reader).

The report came back **0 of 9. 0%.**

Re-running with the corpus's own wording returns a match, so the import and
matching path is correct; this is the 10.9% recall, sampled. But 10.9%
stated as a percentage and 10.9% experienced as "the product told my first
prospect it knows nothing about any of their problems" are different facts,
and only the second one predicts what happens in the room.

That is the single highest-value thing to fix in this codebase, and it is
not a tuning exercise.

## Two tiers, opposite trades — and only one of them is a defect

`python commons/eval/retrieval_tiers.py` runs the *per-org* lexical ranker
(`commontrace/retrieval.py`) over this same corpus and these same probes:

| | Commons (`commons_overlap`) | Per-org (`rank_lessons`) |
|---|---|---|
| Recall on the 46 paraphrased positives | **10.9%** | **84.8% @1, 95.7% @5** |
| Right record present anywhere in output | — | 97.8% |
| The 22 absent failures return *something* | 0% | **100%** |

Same tokenizer. The entire difference is that the commons compares against
a **threshold** and emits covered/not-covered, while the per-org tier
**ranks and returns top-k** with no threshold at all — one shared content
word is enough to put a lesson on the list.

**That is not one tier being broken.** Each made the correct trade for what
it emits. A coverage *percentage quoted to a customer* must not over-claim,
so it buys 0% false positives with recall. A *ranked list a human or agent
skims* must not hide the answer, so it buys recall with the certainty that
something always comes back — and a weak match there costs a glance, not a
wrong decision.

So the corpus is not the problem, and neither is lexical matching: the
answer is present and findable 97.8% of the time. What throws it away is
collapsing that ranking to a binary at a cutoff.

**This corrects two earlier conclusions in this file and in STRATEGY.md.**
The claim below that "no threshold fixes this" is right about the coverage
*percentage* and wrong as a statement about retrieval in general. And
STRATEGY.md §12.4.3's assertion that per-org retrieval is capped by the
same defect is simply false — measured, it is not.

**What it does not license.** Returning ranked candidates instead of a
coverage figure would surface real value that is invisible today, but it
cannot be reported *as* coverage: the top-1 score distributions overlap
(true median 7.0, range 2.5–16.0; absent median 3.0, range 1.5–10.5), so
the score separates on average, not case by case. Any such output has to be
labelled candidates-to-judge, not coverage. The shipped coverage number and
its threshold are unchanged by this finding.

## The honest path forward

Recall is a representation problem, and the fix is semantic rather than
lexical similarity — embedding each failure and comparing vectors. That is
a real change, not a tweak, because **it breaks the current privacy
story**: MinHash signatures are exchanged today precisely because failure
text never leaves the fleet, and an embedding is computed by a model that
has to see the text. Any move in that direction has to answer where the
model runs before it answers whether recall improves. That work is not
done, and this file does not pretend a number exists for it.

## Limits of this evaluation — read before quoting anything above

1. **Same author.** The corpus and the probes were written by the same
   author. Probes are phrased symptom-first, in on-call vocabulary, and no
   probe reuses its target's wording — but shared conceptual framing
   survives paraphrase. Treat 10.9% as an *optimistic* bound on a real
   fleet, not an estimate of one.
2. **Negative controls are the trustworthy half.** Author bias can inflate
   recall; it cannot manufacture a 0% false-positive rate against 22
   failures the corpus does not contain. Weight that result more heavily
   than the recall figure.
3. **Public substrate knowledge, not fleet data.** The corpus is drawn from
   public protocol semantics, vendor documentation and standards. Whether a
   real fleet's recurring failures are substrate failures at all — as
   opposed to failures specific to its own systems — is the load-bearing
   assumption of the entire commons thesis, and this evaluation does not
   test it.
4. **Small numbers.** 46 and 22. A single probe is worth 2.2 points of
   recall and 4.5 points of false-positive rate.
5. **MinHash estimation error.** ~8.8% standard error per comparison at 128
   permutations, on top of the small-sample noise above.

The measurement that would settle any of this is the same one in every
case: run it against recurring failures a customer actually collected. Until
that exists, this file is what is known.

## Addendum: is a better token representation the fix?

`commons/eval/representations.py` tested four alternate tokenizations
(suffix stemming, character 4-grams, character 5-grams, word+4-gram union)
against a fresh held-out probe set (`probes-v2.jsonl`, written after the
original evaluation and never used to shape any candidate) — same corpus,
same MinHash machinery, same 0.30 threshold, only the token set varies. None
beat the shipped word-tokenizer on held-out recall; all traded recall away.
**No change was made** — this was a measurement, not a patch.

The more useful result is diagnostic. Scored independent of the threshold,
the correct record is ranked **first 85% of the time** (93% for stemming).
The matcher is not failing to find the right knowledge — it finds and ranks
it correctly, then a fixed absolute cutoff (0.30) discards the result
because true-match similarity is intrinsically low: paraphrases of the same
failure share few literal tokens even when unmistakably the same failure
(median similarity to the correct record: 0.156). That reframes the defect
away from "the matcher is bad at finding matches" toward "a fixed Jaccard
threshold is the wrong decision rule when literal token overlap this low is
normal for a true match" — which points at ranking/top-k retrieval or a
learned threshold, not a better tokenizer, as the next thing worth trying.
That is still a design decision with the same trust and legal
consequences described above, not a decision this file makes.

Reproduce: `python commons/eval/representations.py`
