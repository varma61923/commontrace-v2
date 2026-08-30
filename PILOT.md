# Running the causal pilot

STRATEGY.md §13 names one test as the cheapest and the most gating: **does
per-org memory measurably cause better outcomes on this fleet's own work?**
Every other link in that case is downstream of the answer. This is how to
run it.

**Want the bundled version of everything below?** `commontrace pilot` runs
all three steps in one command — map the issues into a taxonomy, check how
far the reinforcement loop below has progressed, and measure what changed
(baseline-vs-current resolution rate plus the Impact Dashboard) — and ends
in a yes/no gate that prefers the causal result from this page whenever one
exists. See `commontrace pilot --help`, or `commontrace taxonomy` /
`commontrace impact` for the two report halves on their own. It does not
replace the loop below; it reports on where the loop currently stands.

It is a randomized controlled experiment, not a before/after comparison,
and the difference is the whole point. Every other number this repo
produces — including `reliability`'s `lift` — is correlational, and the
confound is structural: a lesson is retrieved *because* the situation
matched its activation condition, so the occasions where it fired differ
systematically from the ones where it did not. That bias does not shrink
with more data. Withholding at random is what removes it.

---

## Before you start

```bash
commontrace doctor            # exits non-zero if something is actually broken
```

You need a store with active lessons (`commontrace lesson list`), and a way
to give each task a stable id — a ticket number, a run id, a job id.
Anything already unique per task works.

---

## The loop, per task

Three steps. The middle one is your own work; the other two are one command
each.

```bash
# 1. Retrieve, and let the experiment decide this occasion's arm.
commontrace query "<the task, in your own words>" \
    --experiment --occasion-id "TICKET-4711"

# 2. Do the task. If a lesson was injected, it is in the output above.
#    If it was withheld, you get the other arm -- that is the point.

# 3. Record how it turned out, under the SAME id.
commontrace capture \
    --title "<what the task was>" \
    --context "<the situation>" \
    --solution "<what worked>" \
    --agent-type code \
    --occasion-id "TICKET-4711" \
    --resolved          # or --not-resolved
```

**Step 3's `--occasion-id` is what makes the join work.** Without it the
outcome is recorded under a random UUID, nothing matches, and `experiment`
reports every assignment as having no recorded outcome. That was the state
of this pipeline until recently; if you see that message, it is the cause.

Read the result whenever you like — it is cumulative:

```bash
commontrace experiment
commontrace experiment --strict     # non-zero exit if a lesson significantly HURTS
```

---

## How many occasions you need

From the power calculation this repo ships
(`commontrace/experiment.py:minimum_detectable_effect`), at 80% power. Read
it as: *with N occasions in each arm, the smallest true improvement you can
reliably detect is X percentage points.*

| Baseline resolution rate | n=25 | n=50 | n=100 | n=200 | n=400 | n=800 |
|---|---|---|---|---|---|---|
| 40% | 39pp | 27pp | 19pp | 14pp | 10pp | 7pp |
| 60% | 39pp | 27pp | 19pp | 14pp | 10pp | 7pp |
| 80% | 32pp | 22pp | 16pp | 11pp | 8pp | 6pp |

Those are **per arm**, so total occasions is roughly `n / holdout_rate`.

Two things follow, and both are worth knowing before you start rather than
after:

- **A small pilot can only detect a large effect.** At 50 occasions per arm
  you will not see a 10pp improvement, and the report will correctly say
  UNDERPOWERED rather than "no effect". If you need to detect 10pp, plan for
  roughly 400 per arm.
- **The default 10% holdout is tuned for low cost, not fast answers.** It
  spends only 1 occasion in 10 on the control arm, so the control arm fills
  ten times slower than the treated one. `--holdout-rate 0.5` reaches an
  answer far sooner at the cost of withholding help from half the
  occasions. That is a real trade and it is yours to make.

---

## Reading the result honestly

Four verdicts, and the distinctions between them are deliberate:

- **HELPS** / **HURTS** — a statistically significant effect after
  Benjamini-Hochberg correction across every lesson tested. The correction
  matters: at α=0.05 over 100 lessons about 5 look significant by chance,
  and those are exactly the ones that get quoted.
- **NO_MEASURABLE_EFFECT** — tested, nothing found, and always reported
  alongside the minimum detectable effect for that sample. It is a
  statement about the experiment's power, not proof the lesson is useless.
- **UNDERPOWERED** — not enough data yet. Listed separately and
  deliberately, so "we cannot answer this" is never read as "we tested it
  and it does nothing".

If `experiment` warns about unparseable lines in the holdout log, **stop and
explain that before trusting the numbers.** A lost line removes one arm's
data point from a randomized comparison, which biases the effect size
rather than merely widening the interval. Writes are locked, so a non-zero
count means something else wrote to `memory/holdout_log.jsonl`.

---

## What it costs

In the worst case a lesson would have helped and the withheld occasions
lose that help — 1 in 10 at the default rate, 1 in 2 at `--holdout-rate
0.5`. That is the price of knowing, it is bounded, and it is stated in the
CLI output rather than buried here.

A fleet that will not pay it can set `--holdout-rate 0` and keep the
correlational numbers. They should then not be shown a causal claim.

---

## What this proves, and what it does not

It answers whether *these lessons* help *this fleet* on *this kind of
work*. That is link 1 of STRATEGY.md §13, and it gates everything
downstream.

It says nothing about links 3–5: whether value compounds faster than cost
to serve, whether the product survives agent platforms bundling memory, or
whether an operator-curated Knowledge Base is worth the ongoing curation
cost (STRATEGY.md §14 — this is no longer a cross-org network effect).
Those need their own evidence, and §13 names the test for each.

The instrument has been validated against a synthetic effect — seeded at
+45%, recovered as +43% with a 95% CI of [+23%, +64%] — so the statistics
work. Whether any real lesson helps any real fleet is exactly what running
this finds out.
