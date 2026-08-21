# The fork in the road

An engineering-leadership read on what CommonTrace is, what it is being
sold as, and the one number that decides which company this becomes.
Opinionated on purpose. The business calls at the end are not mine to make.

---

## 1. The gap between the positioning and the repository

CommonTrace is positioned around the claim that *lessons compound into
intelligence across the fleet* — a **Shared Intelligence Hub** every agent
connects to, with a **strategic moat** as the terminal business outcome. The
moat argument is a network-effects argument: one org's mistake improves every
other org's agents.

The repository implements something different, and does it well:

| The positioning claims | What the code did |
|---|---|
| Cross-org collective intelligence | Strictly per-org memory. Every read path in `hub/crud.py` was unconditionally scoped to the caller's own `org_id`. |
| A commons that compounds across companies | `Trace.shared_with_commons` existed as a column and **was never read by a single query.** |

That was not an accident or an oversight — it was the correct conservative
call (see `hub/README.md`, "Tenant isolation vs. the cross-org commons
pitch"). Walls first, doors deliberately.

**Status update: the door is now open, and the measurement is live.**
`shared_with_commons` is no longer dead. An org opts a trace in with
`share_trace` (explicit, rationale recorded, revocable, quarantine-blocked),
and any org — contributor or not — can ask `commons_overlap` the question
§5 below says the whole thesis reduces to. The walls held: every original
read path is still org-scoped and `hub/tests/test_tenant_isolation.py`
passes unchanged.

What that changes about this document: §5's "nobody has measured it" is now
a question the product can answer on demand rather than an open research
task, and §6's overlap report is no longer a bilateral file exchange. The
strategic conclusion below is unchanged — the *number* still decides which
company this becomes. It is simply now cheap to obtain.

## 2. Why this is *the* strategic question, not a roadmap item

These are two different companies with two different ceilings:

**(A) Per-org agent memory.** A fleet's own experience compounds for that
fleet. Real value: the Loops numbers (−53% time-to-resolve, −29% churn) are
customer-confirmed and were produced *by the per-org product.* Defensibility
comes from switching costs — their accumulated memory lives here.
Growth is linear in customers. This is a good, fundable, sellable business.
It is a DevTools/observability comp.

**(B) A cross-org knowledge commons.** Value to each customer grows with the
number of *other* customers. That is a genuine network effect, it is
winner-take-all, and it is the shape that produces the outcome the user is
asking about. Comps are Stack Overflow ($1.8B), GitHub, npm — infrastructure
that became the default place a category's knowledge lives.

You cannot get to (B) by doing more of (A). The switch requires an explicit
decision, and (B) has a hard problem (A) does not.

## 3. The hard problem with (B), stated honestly

**Adverse selection.** Why would org A contribute knowledge that helps org
B, who may be their competitor?

The naïve answer ("reciprocity!") fails, because the most valuable lessons
are the most proprietary. If contribution is voluntary and undifferentiated,
orgs contribute their generic lessons and withhold their good ones, and the
commons fills with low-value filler. This is the reason most
"shared industry knowledge base" plays die.

## 4. The insight that makes (B) tractable

**Most agent failures are not competitively sensitive. They are failures of
the shared substrate.**

- "Stripe webhook handlers need idempotency keys" is not a trade secret.
- "React 19 hydrates `Date` differently than 18" is not a moat.
- "This model silently truncates tool output above N tokens" is not IP.

Every fleet on earth rediscovers these independently, at full cost, and
none of them consider the knowledge proprietary. Meanwhile the genuinely
competitive things — your pricing rules, your escalation policy, your
qualification criteria — stay private and always should.

So the commons is not "share everything." It is:

> **Substrate failures are shared. Business logic stays private.**

That line is clean, it is defensible to a security reviewer, and it maps
onto the existing schema with no redesign: `domain` and `tags` already
distinguish these, and `extensions` already namespaces everything
profile-specific. A trace about `git-safety` or `cuda-gpu` is substrate. A
trace whose value lives in `extensions` under your own profile is yours.

## 5. But is the overlap big enough? Nobody has measured it.

> **Superseded in part — read §11.1 with this section.** The measurement now
> exists. It returned 10.9% recall against failures the corpus provably
> contains, which means it measured the *instrument*, not the overlap. The
> decision rule below ("if 40% ... if 4% ...") cannot be applied to that
> number, and §11.4 replaces §9.1's instruction to run it on customer stores.

Everything above is a hypothesis. The entire (B) thesis reduces to one
empirical quantity:

> **Of the failures a new fleet is about to hit, what fraction has some
> other fleet already solved?**

If that number is 40%, the commons is worth building and the network effect
is real. If it is 4%, (B) is a mirage and the honest move is to sell (A)
extremely well.

Nobody knew this number. It was unmeasured, cheap to measure, and being
treated as an assumption. That was the actual bottleneck — not features,
not scale, not the container image.

**It is now a query.** `commontrace commons report` returns exactly this
fraction against the live commons, for any org, contributing or not. The
bottleneck moves from "we cannot measure it" to "we need enough
contributed corpus for the measurement to mean something" — which is a
go-to-market problem with a known shape, not an open research question.

Note the benchmark already contains the *within-org* version of exactly
this question: `transfer_gap` measures whether a lesson learned on project X
helps on project Y (`benchmark/STATUS.md` §2.4). It was mechanically 0% for
a long time because everything was tagged one project. The cross-*org*
version is the same measurement one level up.

## 6. What I built, and why it is the highest-leverage thing available

Two versions of the same measurement, and the difference between them is
the difference between a research instrument and a product.

**`commontrace overlap`** — a bilateral **Fleet Overlap Report**. Answers
"how much would fleet B gain from fleet A's lessons?" **without either
fleet sending the other any lesson text**, by exchanging MinHash signatures
instead of content. Both sides export a file and somebody moves it. Useful
for a controlled two-party study; it does not scale past one.

**`commontrace commons report`** — the same question asked of the Hub, in
one call, against every trace every org has opted into the commons. No
bilateral negotiation, no file exchange, no contribution required first.
This is the version that can sit in a first meeting.

Why this specific thing:

1. **It de-risks the company's biggest decision for near-zero cost.** You
   get the (A)-vs-(B) number before betting the roadmap on it.
2. **The output is itself the strongest sales asset you have.** "We analyzed
   your traces against the commons: 47 of your recurring failures are
   already solved, here are the three costing you the most" is a far better
   first meeting than any claim made on the company's behalf.
3. **It solves the adverse-selection problem's on-ramp.** Nobody has to
   contribute anything to find out what they'd get. The report is the
   incentive to join, which is exactly the bootstrapping mechanism a
   commons needs.
4. **It is measurement, not a bet.** If the number comes back low, you
   learned it in a week instead of a year.

Honest limitation, stated in the code and worth repeating here: MinHash
signatures do not transmit lesson text and text cannot be reconstructed
from them, but this is **not** a cryptographic privacy guarantee — a party
who can guess a candidate string can test whether it is present. That is an
acceptable trade between consenting pilot participants. A production
commons across mutually distrustful orgs needs private set intersection or
differential privacy, and that is called out as a follow-up rather than
quietly assumed.

## 7. The deeper gap: the corpus could not tell a good lesson from a bad one

This one turned out to be more fundamental than "a missing feature", and
`commontrace reliability` now addresses the core of it.

CommonTrace's own headline claim is *"Raw memory remembers. CommonTrace
generalizes."* The first half was delivered — a `Lesson` is a generalized
rule with an explicit activation condition, not a stored episode. The
second half had a hole: **nothing ever checked whether the generalization
was correct.** There was no way to represent, let alone detect:

- a lesson that is simply wrong;
- a lesson whose `applies_when` is too broad, so it fires where it does not
  help;
- two `active` lessons that contradict each other and get injected into the
  same decision.

`Lesson.status` was human-set and never revisited; `Trace.trust` is an
up/down vote count. That made the corpus a **retrieval system** — it
returns what was written, and quality is fixed at authoring time and can
only decay. A learning system also answers *"and was it right?"*.

Everything needed for that second question was already being recorded and
joined by nothing: `lessons_retrieved_by_alpha`, `lessons_hit`, `verdict`,
`Trace.outcome.resolved`, `outcome.repeated_error`.

### Why this is the actual moat

The protocol is copyable in a weekend. Storage is a commodity. What is not
copyable is knowing **which accumulated knowledge is reliable**, because
that judgment is derived from outcome data a competitor does not have. More
usage → better calibration → better retrieval → more usage. That is the
compounding loop the product's positioning promises, and it is a data
advantage rather than a code advantage.

It is also a **precondition for the commons**, not a follow-up to it:
pooling knowledge across organizations amplifies contradiction rather than
averaging it out, because two fleets can hold opposite rules that are each
correct in their own unstated context. A commons that injects contradictory
guidance is worse than no commons.

### What is built, and what is not

Built: outcome-linked credit assignment with Wilson lower bounds (so a
young corpus reads as honestly UNPROVEN rather than falsely good), the
HARMFUL-vs-MISCALIBRATED distinction (a wrong rule and an over-broad
trigger need opposite remedies), and contradiction detection from
activation overlap plus lexical polarity plus empirical divergence.

Not built, and worth knowing:
- Contradiction detection is partly lexical, so it misses conflicts phrased
  without always/never-style markers.
- Every number it reports is **correlational**, including `lift`. §8 is
  about that, and about the intervention that fixes it.
- Nothing feeds these scores back into retrieval ranking yet. Doing so is
  the step that makes the loop actually close, and it should wait until the
  scores have been validated against a real corpus.

## 8. The claim nobody could verify: from correlation to cause

This is the one I would defend hardest, because it is the difference
between a product that reports numbers and a product that can prove them.

**The problem.** Every lesson-value number in this repo before now —
`lift`, `precision`, the published case studies — is correlational, and
the confound is structural rather than a sampling artifact:

> A lesson is retrieved **because** the situation matched its activation
> condition. So the occasions where lesson L fired are systematically
> different from the occasions where it did not.

A lesson that fires on routine work shows beautiful lift while
contributing nothing. A lesson that fires only on the gnarliest incidents
looks harmful while being the reason those incidents got resolved at all.
Observation cannot separate these, and **the bias does not shrink with n** —
collecting ten times the data makes the wrong answer ten times more
confident. I verified this rather than assuming it: in simulation, against
a known ground truth, correlational scoring labeled a genuinely helpful
lesson HARMFUL (−13.7%) and a useless one RELIABLE (+20.0%). Both exactly
backwards. Those two cases are now regression tests
(`tests/test_experiment.py::TestCorrelationVsCausation`).

**Why it matters commercially, not just intellectually.** A prospect's only
path to belief today is a 30-day before/after pilot — which is itself
confounded, slow, and unrepeatable. That is the longest, most fragile part
of the sales cycle. And the published case studies are somebody else's
fleet; nothing lets a buyer verify the effect on *their* work.

**The fix.** `commontrace query --experiment` withholds a lesson from a
random ~10% of the occasions where it was *eligible* — the activation
condition matched — and logs which arm each occasion landed in.
`commontrace experiment` joins those arms to recorded outcomes. That is a
randomized controlled experiment, so the resulting number is causal:

> *"Tasks resolved 34% more often with this lesson injected (95% CI
> [12%, 56%], p = 0.002, n = 412)."*

That sentence survives a technical review. *"Tasks with this lesson tend to
succeed more"* does not.

**Four things it unlocks.**

1. **Proof on the customer's own data, in days rather than a quarter.** The
   pilot stops being a leap of faith and becomes a measurement.
2. **Co-firing lessons become separable.** §7 listed this as the hard case
   with no observational answer. Holdout assignment is independent per
   lesson by construction, so some occasions get A without B and vice
   versa — the tie is broken by design, not by more data.
3. **A safety net for closing the loop.** Letting reliability scores drive
   retrieval ranking (§7's "not built") is only responsible if degradation
   is detectable. This is the detector, and `--strict` is the CI gate.
4. **A defensible pricing story.** Measured causal effect per lesson is the
   most direct denominator for value-based pricing that this product can
   have (§9.4).

**Design decisions worth knowing.** Assignment is a deterministic hash of
`(lesson, occasion, salt)` — no stored state, exactly reproducible when a
result is disputed months later, and stable under retries so an occasion
cannot flip arms by being processed twice. Significance is
Benjamini-Hochberg-corrected across all tested lessons, because at α = 0.05
over 100 lessons ~5 look significant by chance and those are precisely the
ones that end up being quoted. Underpowered comparisons are excluded from
that correction rather than counted in it, and are reported as
UNDERPOWERED — a separate verdict from NO_MEASURABLE_EFFECT, so "we cannot
answer this yet" is never presented as "we tested it and it does nothing".
Every null result carries its minimum detectable effect alongside.

**The honest cost.** In the worst case the lesson would have helped and one
occasion in ten loses that help. That is a real cost, it is bounded, and it
is stated in the CLI output rather than buried. A customer who will not pay
it can set `--holdout-rate 0` and keep correlational numbers — but they
should then not be shown a causal claim.

**What is still open.** The join from occasion to outcome relies on the
caller passing a stable `--occasion-id`; a fleet that does not thread that
id through gets an empty report rather than a wrong one, which is the right
failure mode but still a failure mode. And nothing yet runs the experiment
continuously in the background — it is opt-in per retrieval today.

## 9. Decisions I cannot make for you

Flagging rather than inventing:

1. **(A) or (B)?** ~~Run the overlap report on 2–3 real customer stores
   first. Let the number decide; do not decide before the number.~~
   **Superseded by §11.3/§11.4.** The report was run against a held-out
   set and returned matcher recall rather than overlap, so running it on
   customer stores today would produce a number governed by measurement
   error. The recommendation is now: commit to (A), keep (B) dormant
   behind the gate in §11.4.
2. **Commons terms.** If (B): what is contributed by default, what is opt-in,
   and what does a customer get for contributing? This is a contract
   question with security-review consequences, and it must be settled
   *before* any cross-org read path is written. `DATA_RETENTION.md` §4
   already flags the deletion half of it as unresolved.
3. **Open-source posture.** The protocol becoming a standard and the Hub
   being a business are compatible, but the split has to be chosen
   deliberately.
4. **Pricing.** Per-seat, per-fleet, and per-trace-volume each imply a very
   different product. The operational-cost telemetry now exists to answer
   this empirically rather than by intuition.

## 10. What I would not do

- **Do not build the cross-org read path yet.** It is the one change that
  can leak a customer's data across a tenant boundary, and today's strict
  isolation is an asset in every security review. Build it after the number
  justifies it and the terms in §9.2 are settled — not before.
- **Do not lead with the benchmark numbers.** −53% and 9%→78% are real and
  externally confirmed, and they are per-org results. Citing them as
  evidence for the *commons* would be claiming something they do not show.

---

## 11. Update (2026-08-21): the number came back, and it does not say what §5 expected

§5 said the entire (B) thesis reduces to one measurement, and §9.1 said to
let that number decide. Two things have since arrived. Neither was
available when this document was written, and together they change the
recommendation.

### 11.1 The measurement exists now — and the instrument is the bottleneck

`commons/eval/` runs the shipped corpus against 46 held-out probes written
symptom-first (the vocabulary an on-call engineer actually uses) plus 22
substrate failures deliberately absent as negative controls. At the shipped
threshold:

| | |
|---|---|
| Recall on failures the corpus provably contains | **10.9%** (5/46) |
| Of those matches, the right record | **100%** |
| False positives on the 22 controls | **0%** |

Read §5's decision rule against that: *"If that number is 40%, the commons
is worth building. If it is 4%, (B) is a mirage."*

**Neither branch applies, because 10.9% is not the overlap — it is our
ability to detect the overlap.** Every one of those 46 probes describes a
failure the corpus demonstrably holds. True overlap in that experiment was
100% by construction, and the matcher found one case in nine. So the
honest statement is not "overlap is low." It is:

> We still do not know the overlap. We now know our instrument cannot
> measure it, and we know that precisely.

That is a materially different finding from either branch §5 anticipated,
and it invalidates §9.1's instruction as currently written. Running the
report on 2–3 customer stores today would return a number governed by
matcher recall, not by how much those fleets actually share — and whoever
saw it would reasonably conclude the commons is empty when it is not.

**Why no threshold fixes this.** Recall only becomes useful near 0.10,
where 23% of *absent* failures are reported as present. Precision and
recall do not trade off into a usable operating point anywhere on the
curve, because the problem is the representation: Jaccard similarity over
content words is lexical, and two engineers describing the same substrate
failure share almost no words. The credible fix is semantic similarity, and
it carries a cost this document must name — embeddings require a model to
read the failure text, which retracts the *"failure text never leaves your
fleet"* guarantee the entire privacy story rests on. That is a trust and
legal decision, not an engineering one, and it is unresolved.

### 11.2 The field signal points the same way

Independently of the measurement, the person selling this reports: the B2B
offer being sold today is entirely internal (single-organization
deployments), traction is materially better there than for the commons, and
the explicit instruction is **no cross-org knowledge sharing until traction
is proven.**

That is not a constraint to route around. It is (A) winning on evidence,
from the only source that counts — someone trying to sell both.

### 11.3 Revised recommendation

**Commit to (A) now. Keep (B) as a live option behind a falsifiable gate.
Do not spend on (B) until the gate opens.**

This is a change from this document's original posture, which treated the
choice as pending a cheap measurement. The measurement turned out to be
cheap to *run* and not yet cheap to *trust*, and the market moved first.

What that means concretely, all of it already implemented:

- **(A) is the product.** Per-org agent memory, with `HUB_COMMONS_ENABLED=false`
  making "no cross-org sharing" a property of the deployment rather than a
  promise about behaviour — the three commons tools are absent from the MCP
  surface entirely, not merely unused. `hub/DEPLOYMENT.md` §13 documents the
  single-org deployment as a first-class shape, not a stripped-down mode.
- **(B) stays built but dormant**, so choosing it later is a config change
  and a decision, not a re-architecture. The cost of keeping it is one
  boolean and the tests that pin both modes.
- **The moat for (A) is switching cost, not network effect.** A fleet's
  accumulated, validated, causally-measured memory lives here. §8's
  randomized-holdout machinery is what makes that defensible rather than
  sticky-by-inertia: nobody rips out the thing with a measured effect size
  on their own data.

**Stop citing the network effect in positioning.** Until §11.4's gate
opens, it is an unvalidated hypothesis, and `commons-stats` deliberately
reports contributing-orgs separately from seeded rows precisely so nobody —
including us — can mistake operator seeding for a network effect.

### 11.4 The gate that would reopen (B)

Three conditions, in order. All are falsifiable and none is a matter of
opinion:

1. **The instrument works.** Recall above ~60% on the held-out set in
   `commons/eval/`, with false positives on the negative controls still at
   or near zero. Until then any coverage number quoted to a customer is
   measurement noise. This is the binding constraint and it is a research
   task, not a feature.
2. **The privacy question is answered before the recall problem is fixed,
   not after.** If the answer is embeddings, someone has to decide where
   the model runs and what the customer is told, and that has to be settled
   while it is still a design choice rather than a shipped surprise.
3. **Then, and only then, re-run §9.1** — the overlap report against 2–3
   real customer stores, and let the number decide as originally intended.

If (1) proves intractable, (B) is closed and this becomes an excellent (A)
company. That is not a failure mode; it is the answer §5 asked for,
arriving via the instrument instead of via the corpus.

### 11.5 What the entitlement model is, and what it deliberately is not

`hub/plans.py` implements metered entitlements — storage and commons
queries, free/team/scale, enforced at the query layer with an atomic usage
counter — and **prints no currency anywhere.** That omission is deliberate
and worth defending, because it looks like an oversight.

A price is a claim about value. The one denominator this product can defend
is measured effect on the customer's own data (§8.4) and, if (B) ever
opens, delivered commons hits (`commons-value`). Both are now computable.
Attaching a dollar figure in the repository would encode a number nobody
has agreed to, in the one place people treat as authoritative, and it would
be quoted. So the mechanism ships and the number stays a business decision.

The pricing *hypothesis* worth testing, stated as a hypothesis: for (A),
price against measured resolution-rate improvement per fleet, because that
is the only quantity this product can prove causally and it scales with
the customer's own benefit rather than with seats or trace volume. Whether
the market accepts that shape is unknown and is not answerable from this
repository.

### 11.6 What is still not mine to decide

Unchanged from §9, minus §9.1 which §11.4 now supersedes: commons terms,
open-source posture, and the actual price. Added by this update: **whether
to fund the recall research at all.** It is the only thing standing between
this and a defensible answer on (B), and if the internal B2B offer keeps
outperforming, the rational call may be to leave that question closed and
sell (A) extremely well — which §2 already noted is a good, fundable,
sellable business, and which is the one the evidence currently supports.
