# Effect field: evidence transfer between similar memories

The holdout experiment measures each memory on its own. A fleet that keeps
distilling lessons produces many paraphrases of the same idea, and without
transfer each paraphrase starts from nothing. A reworded harmful lesson
re-enters at the explore rate, and a copy of a proven one is withheld half the
time until it proves itself again. The effect field carries randomized evidence
between memories whose content is similar, but only after the store's own data
shows that content predicts effects.

```sh
unimem allocate gate --dest ./store                          # field diagnostics, always shown
unimem allocate enable --mode thompson-field --dest ./store  # let the gate use it
unimem allocate plan --if-due --dest ./store
```

## Model

`effect_field.fit` places a Gaussian-process prior over memory content and fits
it to the gate's per-memory Hajek effects:

    effect_i = base_i + f(x_i) + u_i
    f ~ GP(0, share * tau^2 * cos(x, x')^power)
    u_i ~ N(0, (1 - share) * tau^2)

- `base_i` is the causal gate's leave-one-out global or source prior.
- `tau^2` is the gate's DerSimonian-Laird between-memory variance.
- The kernel is cosine similarity on smoothed TF-IDF vectors of hashed content
  terms. For lessons these come from the lesson text (name, description,
  applies/does-not-apply fields and body); for other memories they come from
  recorded signatures (below). It is pure Python, offline and deterministic.
- Memories with embeddings are compared in each embedding space as well. A
  memory in several spaces is the concatenation of its unit vectors, each scaled
  by `1/sqrt(spaces)`, so similarity is the mean cosine over the spaces two
  memories share. Different embedding models never compare.
- Integer powers of a cosine Gram matrix are positive semidefinite.

Each memory's prior is the GP's best linear prediction from its 12 nearest
measured neighbours. Its own data is never used. The solve uses the full
covariance between neighbours, so ten near-duplicates add about as much as one
witness, and the variance reduction can never exceed `share * tau^2`. With
`share = 0` the prior is exactly the gate's.

## Memories from any agent, model or robot

Most memories reach UniMem as an id, a revision and content that passes
through: gateway candidates, items behind framework middleware, or skills and
keyframes behind a robot bridge. While a `thompson-field` schedule is in force,
or after `memory_signatures.set_recording(root, True)`, recall stores a
signature for each randomized memory whose revision or embedding changed. A
signature is:

- hashed term counts of the text, at most 256 terms, never the words;
- an optional embedding and its model label, from any encoder: a sentence model
  for an LLM tip, a vision-language encoder for a robot keyframe, or a policy
  embedding for a motion primitive.

The gateway accepts these on each recall item:

```json
{"id": "regrasp-7", "embedding": [0.12, -0.08, ...], "embedding_model": "vla-skill-v1"}
```

Vectors are never echoed back in `deliver`. Python callers put `embedding` and
`embedding_model` on dict items passed to `CausalMemory`.

Signatures are derived from memory content. The canonical erasure cascade
removes them and lists each one in its receipt as `memory-signatures`. Hashed
terms are not encryption: anyone holding a vocabulary can test whether a word
occurred.

Memories whose signature was recorded in the last 30 days and that have no
resolved assignment yet are forecast at the next plan, like unmeasured active
lessons. Recording costs about 1 ms per recall for eight 384-dimension items.
It happens only in field mode.

## Validation

`power` (1, 2 or 4) and `share` (0.2 to 0.95) are chosen by leave-one-out log
predictive density over measured memories that have at least one witness. The
field is `VALIDATED` only when:

- at least 10 such memories exist, and
- the best configuration beats no transfer on a one-sided paired z test at
  alpha 0.05, Bonferroni-corrected over all 15 configurations (z of about 2.71).

Otherwise it is `NOT_VALIDATED` or `INSUFFICIENT`. In that case it changes no
prior, rate or forecast, and a `thompson-field` schedule matches a `thompson`
schedule.

## What a validated field changes

- **Priors.** Measured memories and their contexts start from the field prior
  instead of the base prior.
- **Forecasts.** Active lessons with no assignments yet, but at least one
  measured witness, get a forecast posterior. Their first-era rate is the
  probability-matching rate, clipped to `[floor, 1 - floor]`. Lessons with no
  witness keep the default explore rate.
- **Schedule basis.** The schedule records `mode: thompson-field`, the field
  summary, the kernel digest and every forecast's P(helps). Every rate is
  published before the occasions it governs, so assignment stays randomized and
  the integrity audit and AIPW estimates are unchanged.

`allocate gate` always reports the field, used by the gate or not. The report
includes the verdict and predictive gain, each forecast with its witnesses, and
a separate off-policy check of the field gate. In that check, memories first
seen in the later half are scored at their forecast rates, which is an
out-of-sample test of the forecasts themselves.

## Look-alikes with opposite effects

A similarity kernel blurs pairs that read alike but act in opposite directions,
for example "call the v1 refunds endpoint" and "call the v2 refunds endpoint".
`effect_field.lookalikes` tests similar pairs (cosine of at least 0.6) for a
difference in effect, controls the false-discovery rate with Benjamini-Hochberg,
and reports a pair when the effects differ in sign by at least the practical
threshold. These pairs should be reviewed and reworded so their difference is
explicit.

## Measured benefit

`python -m benchmarks.effect_field_bench --reps 30` simulates a fleet over 8
eras of 250 occasions. Each era it distils two new paraphrases for each of six
lesson families (true effects from -15 to +12 points), and each occasion
retrieves four live lessons. Results (30 seeds, common random numbers):

| Scenario | Policy | Success | Harmful share of injections | Regret / 100 occasions |
|---|---|---:|---:|---:|
| content predicts effect | fixed 50% | 50.4% | 33.2% | 16.9 |
| | thompson | 54.1% | 25.0% | 13.2 |
| | thompson-field | 55.8% | 21.7% | 11.5 |
| placebo (effects shuffled) | thompson | 54.0% | 25.0% | 13.2 |
| | thompson-field | 54.0% | 25.0% | 13.2 |

With content linked to effect, the paired lift of `thompson-field` over
`thompson` is +1.68 ± 0.11 successes per 100 occasions, and regret falls by 13%.
In the placebo run the field never validated, and the result is identical to
`thompson` (lift 0.00).

## Limits

- Forecasts are allocation weights, not evidence. They never set a verdict,
  never withdraw or graduate a memory, and never enter `experiment.analyze`,
  billing, valuation or release evidence.
- The lexical kernel sees shared words, not shared meaning. Paraphrases in
  different vocabulary need embeddings. Opposite instructions that share
  vocabulary are what `lookalikes` exists to surface.
- Signatures exist only for memories recalled with a recording store. Memories
  seen before field mode was enabled join once they are recalled again.
- Embedding similarity uses numpy when it is installed. Without numpy it is
  pure Python and costs O(memories^2 x dimensions) per plan.
- A brand-new memory is forecast from the next plan onward. Within the era it
  first appears in, it keeps the schedule's default explore rate.
- Neighbour residuals use the gate's leave-one-out base, which still includes
  the target memory's evidence at about 1/n weight. The leak is small and biases
  toward the base, not away from it.
- Cost is about 2 s per fit with 1,000 measured lessons on one CPU core (15
  configurations of a 12-neighbour solve per lesson). `allocate gate` fits the
  field up to three times: model, off-policy split and diagnostic section.
- Validation is per plan and uses the current salt's resolved assignments. A
  field can validate in one era and not the next. The schedule basis records
  which happened.
