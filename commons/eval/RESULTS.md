# Commons coverage: measured, not asserted

Reproduce: `python commons/eval/run.py`

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
