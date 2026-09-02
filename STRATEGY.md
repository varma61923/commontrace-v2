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

### 11.4a Update (2026-08-29): the gate's binding constraint was measured, and it does not bind

§11.4 below names three conditions, and calls the first — "the instrument
works", defined as recall above ~60% on the held-out set — *the binding
constraint, and a research task, not a feature*. §11.1 reasoned that
fixing it required semantic embeddings, which would retract the
"failure text never leaves your fleet" guarantee, making condition 2
(settle the privacy question first) unavoidable.

**Measured, that chain does not hold.** §12.7 had already shown that
ranking rather than thresholding recovers the answers — but it showed it
using the per-org ranker, which reads query *text*, so it did not transfer
to the commons. `commons/eval/search_modes.py` measures the version that
does transfer: rank the commons's own **MinHash signatures**, no
threshold, nothing but a signature on the wire.

| | Recall@1 | @5 | @10 | Text leaves the fleet? |
|---|---|---|---|---|
| Threshold (what §11.1 measured) | 10.9% | — | — | No |
| **Signature ranking** | **89.1%** | **95.7%** | **100%** | **No** |
| Text ranking (§12.7) | 84.8% | 95.7% | 95.7% | Yes |

Read against §11.4's own condition 1: **89.1% clears the ~60% bar it set,
and clears it without touching the privacy guarantee** — so condition 2,
which existed because the only known fix was embeddings, is moot for this
surface. Signature ranking even beats text ranking at rank 1.

**What this does and does not change.**

- It does **not** improve the coverage *percentage*. That is still 10.9%
  recall at the shipped threshold, still 0% false positives, and it is
  untouched. Anything quoted to a customer still comes from there.
- It **does** mean the commons can be useful without a trustworthy
  coverage percentage, because the product surface becomes *lookup* —
  "has anyone solved this?", answered with ranked candidates and their
  solutions (`commons_search`) — rather than a number. That is the Stack
  Overflow shape the positioning has always described, and the shape
  §12.7 identified as the open question about the output contract.
- The limit that keeps it honest is unchanged and is why the two tools
  stay separate: absent failures return a non-empty list 100% of the time
  and the score distributions overlap, so ranked results are candidates
  to judge and are never coverage.

So the honest status of the gate is: **condition 1 is met for lookup and
unmet for coverage, and lookup is the surface that makes a commons worth
joining.** Condition 3 — run the overlap report against real customer
stores — is unchanged and still the thing that decides (B), and it is
still gated on corpus, which §12.1 established comes from (A) at scale.
§11.3's recommendation therefore stands: commit to (A), and note that
(B)'s remaining cost just fell from "fund a research programme" to
"accumulate a corpus", which (A) does as a byproduct.

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

---

## 12. The question §2 never asked: is there a large outcome in (A)?

§11 recommended committing to (A). It did so inside §2's frame — (A) is
"linear in customers," a "DevTools/observability comp," and (B) is "the
shape that produces the outcome the user is asking about." I accepted that
dichotomy without testing it. It deserves testing, because two of its
load-bearing claims do not survive contact with what this repository now
contains.

### 12.1 "You cannot get to (B) by doing more of (A)" is false as stated

§2 presents the two as a fork requiring an explicit switch. They are not a
fork. They are a sequence, and the dependency runs in exactly one
direction.

§11.4's gate has two requirements, and §11 only named one. A trustworthy
overlap number needs a working instrument **and** a corpus large enough for
the measurement to mean anything — §5 said as much ("we need enough
contributed corpus for the measurement to mean something") and then the
document stopped treating it as load-bearing.

Where does that corpus come from? Every org running (A) accumulates traces
as a byproduct of using the product. `share_trace` is one opt-in call away
from turning accumulated per-org memory into commons corpus, and
`commontrace commons contribute --tags` already does it in bulk with a
preview and an explicit `--confirm`.

So (A) is not a consolation prize or a hedge. **(A) is the corpus engine
for (B).** A large (A) install base is the single asset that makes (B)
testable at all, and no amount of direct investment in (B) substitutes for
it. That reframes §11's recommendation: committing to (A) is not deferring
the large outcome, it is building the only precondition for it that money
cannot shortcut.

The genuine fork §2 was reaching for is narrower and comes later: *once
corpus and instrument both exist, does the commons open by default or stay
opt-in per customer?* That is a terms question (§9.2), and it is still open.

### 12.2 "Linear in customers" measures the wrong unit

Per-org memory compounds along three axes at once, and only one of them is
customer count:

1. **Agents per fleet.** Every agent connecting to the same Hub reads
   memory every other agent wrote. Within one customer this is already a
   network effect — it is simply bounded by the fleet rather than by the
   market.
2. **Tasks over time.** The corpus grows with usage, and `reliability`
   prunes what does not survive contact with evidence, so the corpus gets
   *better* with age, not merely bigger.
3. **Fleets per customer.** A large enterprise has many, and each is a
   separate expansion.

The expansion motion that shape implies is land-one-fleet, expand-by-agent,
expand-by-fleet. That is the standard enterprise infrastructure shape, and
it is not linear in customers — it is linear in customers multiplied by a
per-customer term that itself grows. Calling it "linear" and comping it to
observability understates it.

**This does not by itself make it a billion-dollar business.** It makes §2's
ceiling argument wrong about which variable to watch. The variable is
agents under management, not logos.

### 12.3 The asset that is actually unusual

Most infrastructure cannot prove its own effect. It is bought on
conviction, defended on conviction, and cut in a downturn on conviction.

§8 built randomized-holdout causal measurement: withhold a lesson from ~10%
of the occasions where its activation condition matched, join the arms to
recorded outcomes, and report a causal effect with a confidence interval,
Benjamini-Hochberg-corrected across all lessons tested, with underpowered
comparisons reported as UNDERPOWERED rather than as "no effect."

That is unusual enough to be worth stating plainly as the strategic asset:

> This product can prove, on the customer's own data, that it works — and
> can be shown to have declined to claim it when the data did not support
> it.

Three consequences, in descending order of confidence:

- **It changes what a renewal conversation is.** "Here is the measured
  effect on your fleet since we last spoke" is a different conversation from
  "here is your usage."
- **It is the pricing basis** (§11.5). Value-based pricing needs measured
  value, and this is the only quantity here that is causal rather than
  correlational.
- **It is hard to copy quickly**, not because the statistics are novel —
  they are not — but because it requires the activation-condition data model
  (`applies_when`), the occasion-level join, and the willingness to publish
  a null result. A competitor bolting memory onto an existing product has
  none of the three and no incentive to build the third.

### 12.4 Where the large-outcome case is weakest — stated, not buried

An affirmative case that omits these is not worth reading:

1. **The platforms may bundle it.** The most likely competitive outcome is
   not a startup; it is agent platforms shipping adequate built-in memory.
   The defence is being provider-agnostic — `install` targets Claude Code,
   Cursor, Devin, Windsurf and generic MCP, and the Hub needs no
   CommonTrace-specific SDK (`PROTOCOL.md` §8) — so the product is the
   layer *across* platforms rather than a feature of one. That is a real
   defence and an unproven one.
2. **One confirmed customer.** §2 cites −53% time-to-resolve and −29% churn
   as customer-confirmed. That is one fleet. §10 already says not to lead
   with those numbers as evidence for the commons; they are also thin
   evidence for the category. `--experiment` exists precisely so customer
   two onward produces its own causal numbers instead of inheriting these.
3. ~~**Retrieval is the same bottleneck one level down.**~~ **Measured, and
   this was wrong — see §12.7.** I asserted by analogy that per-org
   retrieval shares the commons matcher's defect. It does not: on the same
   corpus and the same probes it gets 84.8% recall@1 and 95.7%@5, against
   the commons matcher's 10.9%. (A) is not capped by this. What remains
   true is narrower and is stated in §12.7.
4. **"Substrate failures are shared" is still a hypothesis** (§4). It is
   plausible and unvalidated, and §11.1 established we cannot currently
   measure it.

### 12.5 What I will not do, and why that is not evasion

I will not put a TAM figure, a competitor's pricing, or a funding comp in
this document. Not because those are someone else's job, but because I
cannot verify them from here, and an unverifiable number in a repository is
worse than no number — it acquires authority by being written down and gets
quoted back as fact. That discipline is the same one that produced §11.1's
10.9% instead of a flattering headline.

What I was wrong about in the previous revision is treating that as licence
to skip the analysis. Market *structure* is analysable without inventing
figures, and §12.1–12.4 is that analysis. The distinction: I can reason
about which variable determines the ceiling; I cannot tell you the ceiling.

### 12.6 Revised bottom line

§11.3 said commit to (A) and gate (B). That stands. What changes:

- **(A) is not the smaller bet.** It is the precondition for (B) and the
  only one that cannot be bought. Sequencing, not sacrifice.
- **The metric to run the company on is agents under management**, not
  customer count — that is the variable §12.2 identifies as compounding.
- **The differentiator to sell on is causal proof on the customer's own
  data**, not the network effect (which §11.3 already said to stop citing).
- **The highest-value open question is the commons OUTPUT CONTRACT, not
  retrieval recall** — see §12.7. Measurement moved this: recall is fine
  where it is ranked and poor only where it is thresholded, so the research
  §11.6 called optional is narrower and cheaper than it looked.

Whether that adds up to a billion-dollar company is not something this
document can assert, and any version of it that did would be the kind of
claim the rest of this file exists to avoid making. What it can say is
which variable to watch, which asset is genuinely rare, and which single
engineering result would move both.

### 12.7 Correction: I measured the claim in §12.4.3 and it was false

§12.4.3 asserted that per-org retrieval "runs on the same lexical
machinery" as the commons matcher and is therefore capped by the same
defect, and §12.6 promoted that to the highest-value open question. I
reached it by analogy — same tokenizer, therefore same problem — and did
not measure it. `commons/eval/retrieval_tiers.py` measures it, on the same
corpus and the same probes:

| | Commons | Per-org |
|---|---|---|
| Recall on the 46 paraphrased positives | 10.9% | **84.8% @1, 95.7% @5** |
| The 22 absent failures return *something* | 0% | **100%** |

The tokenizer is shared. Nothing else that matters is. The commons
compares Jaccard against a **threshold** and emits covered/not-covered; the
per-org tier **ranks and returns top-k** with no threshold, so one shared
content word is enough to surface a lesson.

**Neither tier is broken.** Each made the correct trade for what it emits.
A coverage percentage quoted to a customer must not over-claim, so it buys
0% false positives with recall. A ranked list a human or agent skims must
not hide the answer, so it buys recall with the certainty that something
always comes back — where a weak match costs a glance, not a wrong
decision.

**What this changes strategically, in order of consequence:**

1. **(A) is not capped by a retrieval defect.** §12.4.3 named that as the
   most important open question in this document; measurement removed it.
   The per-org product's core loop — find the relevant memory when the task
   is described in the operator's own words — works.
2. **(B)'s problem is smaller and cheaper than §11.1 concluded.** That
   section reasoned from 10.9% to "the representation is wrong, and the
   credible fix is semantic embeddings, which retracts the privacy
   guarantee." That inference no longer holds unchallenged: the corpus
   contains the answer and lexical ranking finds it 97.8% of the time. What
   discards it is collapsing the ranking to a binary at a cutoff. Embeddings
   may still be worth it; they are no longer *required* to get value out of
   the commons, and §11.4's gate should be re-derived rather than assumed.
3. **The honest open question is the output contract, not the matcher.**
   Should `commons_overlap` return ranked candidates alongside — never
   instead of — the coverage figure?

**The constraint on (3), which is why it is a question and not a patch.**
The top-1 score distributions overlap (true median 7.0, range 2.5–16.0;
absent median 3.0, range 1.5–10.5). The score separates on average, not
case by case, and every absent failure still returns something. So ranked
candidates can be offered as *candidates to judge* and can never be
reported as coverage. Shipping them mislabelled would recreate exactly the
over-claiming the threshold exists to prevent. The shipped coverage number
and its threshold are unchanged by this finding, and I have not touched
either.

**Why this correction is in the document rather than quietly fixed.** The
discipline this file runs on is that a claim is worth what its evidence is
worth. §12.4.3 was an inference presented with the same confidence as
§11.1's measurement, and it was wrong for a reason worth remembering:
sharing a component is not sharing a failure mode. Deleting it would remove
the evidence that the method works.

---

## 13. The affirmative case, stated as a case

This section exists because §12 stopped one step short. It analysed which
variable sets the ceiling, which asset is rare, and what the bottleneck
was — and then declined to assemble those into an argument, on the grounds
that a repository cannot assert an outcome.

That conflated two different things. *"This will be a large company"* is a
prediction, unevidenced, and correctly refused. *"Here is the case that it
could be, what each link requires, and what would break it"* is ordinary
strategic work that invents nothing. Refusing the second because it
resembles the first is not rigour, it is avoidance. So: the case, with its
premises exposed and its falsifiers attached.

### 13.1 The arithmetic, which is an identity and not a claim

Revenue is agents under management × price per agent per year. §12.2
established the first term is what compounds here — value grows with agents
per fleet, tasks over time, and fleets per customer, so customer count is
the wrong denominator.

That identity permits very different shapes, and naming them is not the
same as picking one: a large outcome needs either a very large number of
agents at a low per-agent price, or a modest number at an enterprise price.
**Which of those is reachable is exactly what nobody in this repository
knows**, and it is the first thing a real pilot would measure. What the
identity does establish is what to instrument: the Hub already meters
per-org usage (`hub/plans.py`, `manage usage`), so agents-under-management
is measurable from day one rather than reconstructed later.

> **Correction (2026-08-29): that last sentence was false, and it was false
> in the same way §13.2's "the machinery already ships" was.** The Hub did
> meter per-org usage — but it metered *traces stored* and *commons
> queries*. It had no concept of an agent at all. `Trace.agent_type` is a
> CATEGORY (`support`, `sales`, `code`), so a fleet of 25 support agents
> shared one value and was indistinguishable from one agent; `grep` for
> `agent_id`/`max_agents` across the repository returned nothing. **The
> variable §12.6 concludes the company should be run on could not be
> computed, and the per-agent tiers in the sales deck — 5 / 25 / unlimited
> — were unenforceable**, since `Plan` had no agent field and `crud.py`
> checked no agent limit.
>
> This is now built: `Trace.agent_id`, `Plan.max_agents`, and
> `crud.agents_under_management`, reported per org by `manage usage`.
> Two design choices are load-bearing and are stated here because they
> change what the number means:
>
> - It counts agents **active in a trailing 30-day window**, not distinct
>   agents all-time. An all-time count can only rise, so it could never
>   show churn and would bill a customer forever for an agent they ran once
>   and decommissioned. This number can fall, which is the point of it.
> - It is a **floor, not a total**, for any org whose clients do not send
>   `agent_id` — those traces collapse into one `unattributed` agent
>   however many really produced them. `manage usage` marks such orgs with
>   a trailing `+` rather than quoting the number as exact. Same discipline
>   as the commons coverage figure, and for the same reason.
>
> What this does **not** do is make the §13.1 identity answerable. It makes
> the first term countable. Which shape — many agents cheap, or few agents
> expensive — is reachable is still exactly what nobody here knows, and
> still the first thing a real pilot would measure. The difference is that
> a pilot can now measure it instead of estimating it afterwards.

### 13.2 The chain, in dependency order

**Link 1 — per-org memory delivers measurable value.** *Status: evidence
for, one customer.* §2 cites −53% time-to-resolve and −29% churn,
customer-confirmed. §8's randomized-holdout machinery exists so customer
two onward produces its own causal number instead of inheriting that one.
*Falsifier:* run `--experiment` on the next two fleets; if effects are null
or underpowered at reasonable n, the product does not work and nothing
downstream matters. This is the cheapest falsifier in the document and it
should be run first.

> **§13 originally said "the machinery already ships." That was wrong, and
> checking it found the blocker.** `query --experiment` logged an arm under
> an occasion id and `experiment` joined it via the trace's `id` — but
> `capture` had no flag to set that id, so nothing ever joined and every
> assignment was reported as having no recorded outcome. The falsifier this
> section calls cheapest and most gating **could not be run at all.**
> `capture --occasion-id` now closes the loop, and the pipeline was verified
> end to end against a synthetic effect: seeded at +45%, it recovered
> +43% with a 95% CI of [+23%, +64%] and p<0.001, so the interval covers the
> true value. That validates the instrument. It says nothing about any real
> lesson — which is the point of running it on a real fleet.

**Link 2 — retrieval finds the right memory when a task is described in
the operator's own words.** *Status: measured, holds.* 84.8% recall@1,
95.7%@5, 97.8% findable (§12.7). This was the link I wrongly believed was
broken. *Falsifier:* recall on a real fleet's own corpus, which is larger
and messier than 46 curated records.

**Link 3 — value compounds within a customer faster than it costs to
serve them.** *Status: unmeasured, and the weakest link nobody has looked
at.* The compounding argument (§12.2) is structural, not measured. Cost to
serve is knowable — the Hub has connection pooling, per-org metering, and
operational telemetry. *Falsifier:* per-org gross margin at 10× current
scale. If serving cost grows with corpus size faster than value does, this
is a services business wearing infrastructure clothes.

**Link 4 — the layer survives platforms bundling memory.** *Status:
argued, unproven.* The defence is being the layer *across* providers rather
than a feature of one: `install` targets Claude Code, Cursor, Devin,
Windsurf and generic MCP, and the Hub needs no CommonTrace-specific SDK.
*Falsifier:* a customer consolidating onto one agent platform and dropping
this because the built-in memory is adequate. One such loss is signal; two
is the answer.

**Link 5 — the commons opens, and the network effect is real.** *Status:
gated, and cheaper than §11.1 concluded.* (A) at scale generates the corpus
(§12.1); §12.7 showed the matcher can already find answers 97.8% of the
time and the defect is the output contract, not the representation.
*Falsifier:* §11.4's gate, re-derived per §12.7.

**Links 1–4 are the (A) case and do not require link 5.** Link 5 is
upside, and §11.3's recommendation stands: do not spend on it until the
gate opens.

### 13.3 What makes this more than a good DevTools business, if it holds

One thing, and it is link 2 plus §12.3 combined: **a product that retrieves
the right prior experience reliably, and can prove causally on the
customer's own data that doing so changed the outcome.**

Most infrastructure is bought on conviction and cut on conviction. A
renewal conversation that opens with a measured effect size on the
customer's own fleet is a structurally different conversation — and the
same machinery makes value-based pricing possible rather than aspirational
(§11.5). That combination is hard to copy quickly: it needs the
activation-condition data model, the occasion-level join, and a willingness
to publish nulls. A competitor bolting memory onto an existing product has
none of the three and no incentive to build the third.

### 13.4 The honest discount

Link 1 rests on one customer. Link 3 is unmeasured and is where businesses
of this shape usually die. Link 4 is an argument, not a result. The
strongest single objection to everything above is that **agent platforms
bundle adequate memory and this becomes a feature rather than a layer** —
and no amount of engineering in this repository answers that; only
customers choosing it over a bundled alternative does.

### 13.5 What this is, precisely

This is a case with five explicit premises, of which one is measured, one
is evidenced by a single customer, two are argued, and one is gated. It is
falsifiable at every link, and §13.2 names the test for each.

It is not a forecast, and the difference matters more than it might appear.
A forecast asserts an outcome and gets quoted. A case says *if these five
things hold, the outcome is reachable; here is how to find out whether they
hold, cheapest first.* The second is worth something precisely because it
can be wrong in a way you can detect early — which is the same property
that made §12.7's correction possible, and the same discipline that
produced 10.9% instead of a number that would have read better.

Run link 1's falsifier first. It is the cheapest, it gates everything
downstream, and as of `capture --occasion-id` the loop actually closes —
which it did not when this section was written, and which nobody would have
discovered without trying to run the thing the section recommends.

---

## 14. Update (2026-08-30): the org-to-org commons is retired — §3's problem is resolved, §12.1's claim is not

Every section above through §13 was written assuming the shape §3 named:
org A opts a trace in, org B's queries can match it, and the hard problem
is adverse selection (§3) — why would an org contribute knowledge that
might help a competitor? That shape is now gone from the codebase.
`share_trace`/`unshare_trace` and `commontrace commons contribute` do not
exist any more. There is no tool, customer-facing or otherwise, by which
one org's trace can ever become visible to another org.

**What replaced it.** A single corpus the *operator* authors and curates —
`hub/manage.py commons-seed` is the only thing that ever writes to it —
optional per org (`commons_access`, a plan setting) and removable per
deployment (`HUB_COMMONS_ENABLED=false`). Closer to a vendor-maintained
Stack Overflow or wiki than to anything shared between customers. See
`hub/commons.py`'s module docstring for the full reasoning.

**Why, stated plainly rather than re-derived:** orgs do not share their IP
and data with each other. Asking them to was solving a problem nobody
actually has a reason to opt into, adverse selection or not.

### 14.1 §3 is not mitigated, it is dissolved

§3 asked how to design incentives so that voluntary contribution does not
fill the commons with filler. There is no answer to that question that
survives contact with self-interest, which is exactly what §3 already
concluded. The fix taken here is not a better incentive — it is removing
the thing the incentive was for. There is no contribution decision for any
org to face adverse selection about, because no org is ever asked to
contribute. This is not a workaround; it is the honest reading of §3's own
conclusion, taken to its actual end instead of designed around.

### 14.2 §12.1's load-bearing claim is false under the new architecture

§12.1 argued **"(A) is the corpus engine for (B)"**: every org running (A)
accumulates traces as a byproduct, and `share_trace` / `commons contribute`
were one call away from turning that accumulation into commons corpus. That
mechanism is exactly what no longer exists. Running (A) at any scale now
produces **zero** bytes of Knowledge Base content — corpus growth requires
the operator to deliberately author and run `commons-seed`, which is
editorial work, not a byproduct of usage.

This reopens the question §12.1 believed it had closed: where does the
corpus come from? The honest answer now is **operator labor**, not
customer adoption. That is a real, ongoing cost this business must fund
directly (writing and maintaining substrate knowledge, the way a vendor
maintains documentation) rather than one that "falls out" of selling (A)
well. It does not scale with the install base the way §12.1 described; it
scales with however much the operator is willing to write and curate.

### 14.3 What survives measurement, unchanged

The instrument numbers in §11.1 and §11.4a are properties of the *matching
algorithm* against whatever corpus exists, not of who populated that
corpus. They hold exactly as measured:

| | Recall@1 | @5 | @10 | Text leaves the fleet? |
|---|---|---|---|---|
| Threshold (§11.1) | 10.9% | — | — | No |
| Signature ranking (§11.4a, shipped as `commons_search`) | 89.1% | 95.7% | 100% | No |

An operator-curated corpus of the same size and quality as an org-contributed
one would score identically on both. What changed is the source and the
growth curve of the corpus, not the retrieval properties measured against it.

### 14.4 §11.4's gate, re-read

Condition 1 ("the instrument works") is unaffected — met for lookup per
§11.4a, unmet for a trustworthy coverage percentage, exactly as before.
Condition 3 ("corpus large enough to mean something") is the one §12.1
mis-costed: it is no longer a byproduct of (A) at scale, it is a direct,
funded, ongoing editorial commitment. §11.3's recommendation to commit to
(A) still stands on its own terms — (A) remains the whole product for
customers who never touch the Knowledge Base — but it no longer doubles as
an argument for how (B) gets its corpus. Those are now two separate
investments, not one.

### 14.5 §11.5's pricing denominator loses a term

§11.5 named two computable denominators for pricing: measured
resolution-rate improvement (§8.4), and "delivered commons hits
(`commons-value`)" if (B) ever opened. `commons-value` — the per-org
ledger of what an org shared versus what that delivered — no longer exists,
because there is no org contribution to have a ledger about. `hub/manage.py
kb-stats` replaces it with a content-quality report (which Knowledge Base
entries are actually earning their query traffic), but that is an operator
diagnostic, not a customer-facing pricing denominator: no customer
contributed anything to be credited for. §11.5's surviving pricing
hypothesis is the first one alone — price (A) against measured
resolution-rate improvement per fleet — and it was already the stronger of
the two.

### 14.6 What this does not touch

Links 1–4 of §13.2 are per-fleet or per-platform claims about (A); none of
them mentioned the commons and none of them are affected. §12.2–§12.7's
reasoning about (A)'s own economics and retrieval quality is unchanged.
This update is scoped entirely to link 5 and to §3's problem statement —
both of which were always the (B) side of the fork, and (B) remains, as
§11.3 already concluded, not the thing to spend on next.

---

## 15. Update (2026-08-30): a reviewed community-submission channel, not a network effect reopened

§14.2 identified a real cost of retiring org-to-org sharing: corpus growth
stopped scaling with (A)'s install base and became direct, funded operator
labor instead — writing and citing substrate knowledge one entry at a
time. This update adds a second source, `submit_kb_entry` +
`hub/manage.py review-submission`, without reopening §3's problem or
walking back §14's retirement.

**The mechanism.** An org may propose an entry; nothing is published by
that call. It writes to a table (`KnowledgeBaseSubmission`) neither
`commons_overlap` nor `commons_search` ever reads. Only an operator's own
`review-submission` action can turn an *accepted* proposal into a real
`Trace(commons_source='seed')`, owned by the operator, never by the
submitter. Acceptance also awards a permanent, one-time addition to the
submitting org's Knowledge Base query allowance
(`Organization.bonus_commons_queries`); a rejected or still-pending
proposal awards nothing.

**Why this does not repeat §3's failure.** §3's naive fix ("reciprocity")
fails because credit for the ACT of sharing rewards volume: an org keeps
its best lessons and contributes filler to collect the reward. Credit for
ACCEPTANCE changes what is being rewarded. Submitting costs nothing and
proves nothing; only content an operator judged worth publishing earns
anything, so filler is not a viable strategy for extracting allowance the
way it would be under a credit-for-sharing rule. This does not make an
org's incentive to withhold its most differentiated knowledge disappear —
nothing could, and nothing here claims to. What it produces is content
self-selected for being non-competitive enough to clear a human's review,
the same category §14 already described the operator's own seeded
content as: Stack-Overflow-shaped, not trade-secret-shaped.

**What this is not.** It is not §12.1's retired thesis restored. §12.1
claimed corpus size scales with (A)'s adoption automatically, as a
byproduct of usage. This channel scales with review THROUGHPUT, which is
still an operator-side constraint, not a customer-side one — nobody's
corpus grows merely because more organizations run (A). What changes from
§14.4's "direct, funded, ongoing editorial commitment" framing is narrower
than a network effect: reviewing a submitted write-up (read, judge,
accept/reject) is cheaper operator labor than authoring one from nothing
(write, verify, cite a source), so this raises plausible curation
throughput without changing who gates quality or removing the labor
constraint entirely. Call it a labor multiplier for the operator, not a
network effect — a real but modest claim, and the honest one.

**§14.5's pricing correction, refined further.** `bonus_commons_queries`
reintroduces a customer-facing incentive number, but it is not
`commons-value` reborn as a pricing denominator. `commons-value` measured
a two-sided commercial relationship: value a customer delivered to other
customers, arguable as a basis for revenue share. This is one-sided: an
org gets query allowance for helping build the *operator's own* product
content, closer to a loyalty credit or a paid-in-product bounty for
editorial labor than to IP licensing. It does not change §11.5's surviving
pricing hypothesis (price (A) against measured resolution-rate
improvement); it is a retention/engagement mechanic sitting next to that
pricing, not a substitute for it.

**§11.4's gate, condition 3, updated once more.** Corpus size is now
funded by two paths instead of one: direct operator authorship (unchanged
from §14.4) and reviewed community submissions (this section). Both are
operator-labor-bound; neither is the free, adoption-driven growth §12.1
originally assumed. The gate's status is otherwise unchanged from §14.4:
condition 1 (instrument works) is met for lookup, unmet for a trustworthy
coverage percentage; condition 3 (corpus large enough) remains a funded
commitment, now with a second, likely cheaper channel feeding it.

---

## 16. Update (2026-08-30): maintenance, and the objection §15 left standing

§14 retired the org-to-org commons and put the operator in charge of the
corpus. §15 added a second way for content to enter it (reviewed community
submissions) and priced the honest claim carefully: a labor multiplier, not
a network effect, because review throughput is still an operator-side
constraint.

Both of those are about **growth**. Neither addressed the objection that
does the most damage to the operator-curated model, which is about
**maintenance**:

> Every entry ever published has to stay true forever, or the product
> degrades in a way that is invisible from the inside. At 100 entries the
> operator can re-read them. At 100,000 nobody can. So curation cost is
> O(corpus), and the model has a ceiling somewhere well below the corpus
> size the §11.4 gate needs.

That argument is correct as far as it goes, and it is the one a technical
diligence would reach for. What makes it wrong is a step it assumes
without stating: that *finding* the bad entries is the expensive part.

### 16.1 Usage already generates the maintenance signal

It was being collected and thrown away. Every `commons_overlap` match
credits `Trace.commons_hits` — the operator's own record of which entries
are actually reaching real failures. Every fleet that tries an answer can
`vote_trace` on it, with a `feedback_tag` that says what kind of wrong it
was (`outdated`, `wrong`, `security_concern`). `trust` was computed from
those votes on every cast and then read by nothing but a tie-break;
`feedback_tag` was consulted nowhere at all.

So the corpus already knew which entries were failing and roughly how
badly, and nothing looked. `hub/commons.py:entry_standing` and
`hub/manage.py kb-review` are the read side of data that was already
there.

The consequence is the part that answers the objection: an operator does
not review the corpus, they review a **queue ordered by damage done** —
security flags, then disputed entries, then expired ones, then dead
weight, each ranked by how much traffic it is affecting. Review cost
therefore tracks the **error rate**, not the corpus size. A 100,000-entry
corpus with a 0.5% error rate is 500 decisions, arriving continuously,
pre-sorted by urgency. That is a staffed function, not an impossible one.

This is the same mechanism that lets Stack Overflow and Wikipedia stay
usable at a scale no editorial staff could read: readers find the errors,
editors adjudicate them. §15 drew the Stack Overflow analogy for the
*contribution* half and was careful to under-claim it. This is the half of
the analogy that actually transfers, and it transfers for a reason the
contribution half does not — flagging a wrong answer costs a fleet
nothing and helps it directly, so the incentive problem §3 identified for
contribution simply does not arise for maintenance.

### 16.2 What it deliberately does not do, and why that is the point

The obvious next step from "the crowd flags errors" is "so let the crowd
remove them." That step is not taken, at any vote count.

Disputed content stops counting toward the coverage figure and sorts last
in lookup. Both of those make this product's own claims *smaller*. Neither
removes anything, and `hub/tests/test_kb_standing.py:TestVotesNeverRetract`
pins it. A corpus where three downvotes silently delete the operator's
content is a corpus a competitor can edit, and the asymmetry between "make
our claims more conservative" (automatic, safe in every direction) and
"remove our content" (a human, audited) is the whole design.

The one place a single vote is acted on is `security_concern`, and what it
does is raise queue priority. Reading a spurious report costs a minute;
missing a real one means bad security advice served from a corpus
customers were told to trust, to every fleet whose failure matches it, for
as long as nobody looks.

### 16.3 What this changes about the gate, stated narrowly

Less than §16.1 might suggest, and saying so is the point of this section
being short.

- **Condition 1** (the instrument works) is untouched. Standing does not
  affect matching at all.
- **Condition 3** (corpus large enough, and the overlap number that
  decides (B)) is affected only indirectly. Corpus growth is still
  operator-labor-bound, exactly as §15 concluded. What changes is that the
  corpus's *carrying cost* no longer grows linearly with its size, which
  removes a ceiling on how large a corpus one operator can responsibly
  hold — a precondition for condition 3, not progress toward it.

The coverage figure will now read slightly lower wherever a disputed entry
was previously counted. That is a real, deliberate reduction in a number
this document has repeatedly said is the one thing customers may quote,
and it is the right direction: §11.1's whole finding was that the
instrument under-reports, and every correction since has moved the number
toward honesty rather than away from it.

### 16.4 The remaining objection this does not answer

Standing depends on fleets voting. Nothing in the product makes them, and
nothing measures whether they do. `kb-stats` will report a corpus almost
entirely `unproven` for as long as query volume is low, which is a truthful
reading and a useless one — an `unproven` corpus is indistinguishable from
an un-consulted one. Both of the automatic consequences degrade gracefully
in that state (an unproven entry counts as coverage and ranks normally, as
it should), so nothing breaks; the maintenance loop simply does not start
turning until there is real traffic.

That is the same dependency §11.4's condition 3 already has, and it is
still gated on the same thing: adoption of (A). Nothing here changes
§11.3's recommendation.

---

## 17. Update (2026-08-30): the moat was a claim about a number the service could not compute

§11.3 settled the strategic question and named the defence:

> **The moat for (A) is switching cost, not network effect.** A fleet's
> accumulated, validated, causally-measured memory lives here. §8's
> randomized-holdout machinery is what makes that defensible rather than
> sticky-by-inertia: nobody rips out the thing with a measured effect size
> on their own data.

§11.5 then named the only pricing denominator this product can defend:
measured resolution-rate improvement per fleet, "because that is the only
quantity this product can prove causally."

Both sentences are about a number. Until this update, **the Hub could not
compute it.**

### 17.1 What was actually there

`Trace.outcome` has carried the five business-outcome fields since the
schema was written — `resolved`, `escalated`, `repeated_error`,
`frustration_signal`, token and call cost — plus `baseline`, a flag
marking traces captured before lessons were being injected. Every
`contribute_trace` writes all of it. A deployment running for a year holds
a complete before/after dataset per customer.

The Hub read that column in exactly two places: it copied it onto the wire
projection, and it carried it forward on amend. It computed nothing.

So the moat argument reduced to: *a customer who thought to run a local
CLI command, against files on their own disk, could see a version of the
number.* The service holding the data could not. Neither could the person
selling it, at renewal, about the customer in front of them.

That is the third time this pattern has appeared in three consecutive
passes over this codebase — `Trace.trust` computed and read only as a
tie-break (§16), `Vote.feedback_tag` recorded and consulted nowhere
(§16), and now `Trace.outcome` written on every call and never once
aggregated. The recurring shape is worth naming, because it is not
sloppiness: each of these was collected *correctly and early*, by someone
who understood it would matter, and then nothing was built on top because
the collecting felt like the hard part. It is not. Collecting a signal is
the cheap half; the expensive half is being willing to publish what it
says.

### 17.2 Why it had to be built defensively rather than persuasively

This is the number that ends up in a renewal conversation, a board deck,
and eventually a diligence memo. The temptation in every design decision
is toward the flattering reading, and every one of them was taken the
other way:

- **The correction is applied.** Four metrics at α=0.05 means roughly one
  in five fleets shows a "significant" result by chance. Benjamini-Hochberg
  across the four is what stops the lucky one being the one that gets
  quoted. `hub/tests/test_fleet_outcomes.py` pins this with a fixture tuned
  to sit inside the window where corrected and uncorrected disagree, so the
  test fails if the correction is ever quietly dropped.
- **`worsened` is a first-class verdict**, at equal prominence, never
  sorted below the wins. A measurement instrument that can only return
  good news is not one, and §11.3's argument depends on this being a
  number a customer can trust *against the operator's interest*. If it
  cannot say "you got worse", it cannot credibly say "you got better".
- **Nulls report their own power.** "No significant improvement" from 60
  traces and from 60,000 are the same string and opposite facts. Every
  inconclusive row carries the minimum effect that sample could have
  detected, so an early customer reads "cannot answer this yet" rather
  than "the product does nothing".
- **The report explains its own apparent contradiction.** A row can show a
  95% CI excluding zero next to a `no change` verdict, because the
  interval is uncorrected and describes one metric while significance is
  judged across four. Both numbers are right. Left unexplained, a careful
  reader concludes the instrument is broken — which is worse than either
  number alone.

### 17.3 The claim this does NOT support, stated as loudly as the code states it

**It is a before/after comparison. It is not causal, and it must never be
described as one.**

`baseline` marks a time window. A model upgrade, a shift in task mix, a
seasonal change in what customers ask, or a team simply getting better at
its job all sit inside that window alongside anything this product did,
and no amount of statistics applied to two buckets separates them.

§11.3's own wording — "causally-measured memory" — refers to §8's
randomized holdout, which withholds lessons at random so the arms differ
only by the treatment. That is a different and stronger design, it is
already implemented in `commontrace/experiment.py`, and this new surface
deliberately borrows its statistics while refusing its language.
`OBSERVATIONAL_CAVEAT` is returned on every response and printed on every
operator run for exactly this reason.

The distinction is not pedantry, it is the difference between a durable
claim and one that dies in a single meeting. "Our customers improved 23%"
is demolished by the first person who asks what else changed that quarter.
"Our customers' recorded resolution rate rose 23% since their baseline
window, observed not causal, and here is the randomized design that would
settle it" survives that question, and is the only version worth building
a company's evidence base on.

### 17.4 What this changes, narrowly

- **§11.3's moat argument becomes checkable** rather than aspirational. A
  customer can ask the question themselves, over the same MCP surface
  their agents already use, unmetered.
- **§11.5's pricing hypothesis becomes testable.** You cannot price
  against a number you cannot compute; now it can be computed per fleet,
  per month. Whether the market accepts that pricing shape is still
  unknown and still not answerable from this repository, and no currency
  figure appears anywhere here.
- **The operator gets a leading indicator.** `usage` and `revenue` report
  consumption, which looks healthy right up to a renewal a customer
  declines. `manage.py outcomes` reports whether each fleet's own numbers
  are moving. It refuses to correct across orgs and says so, because
  scanning fifty customers and quoting the three that came back
  significant is a further multiple-comparisons problem no per-report
  correction can fix.

### 17.5 What it does not change

The evidence base is still thin, and this does not thicken it — it builds
the instrument that could. §11.2's field signal (one customer, internal
B2B deployments, traction better there than for the commons) is unchanged.
§11.4's gate on (B) is untouched; none of this bears on the commons
question at all.

And the honest limit on the instrument itself: it needs fleets to have
recorded a baseline window, and a fleet that never ran one gets a report
saying so rather than a number. That is the correct behaviour and it is
also a real adoption cost — the most valuable measurement this product can
make requires a customer to have instrumented *before* they saw any value
from it, which is precisely when they are least motivated to. The
randomized holdout has no such requirement and is the better answer for
anyone starting today; this surface is what makes the year of data an
existing customer already has worth something.

---

## 18. Update (2026-08-30): link 3 was the weakest link, and its cost half now has a number

§13.2 lists five links the (A) case rests on and marks exactly one
**"unmeasured, and the weakest link nobody has looked at"**:

> **Link 3 — value compounds within a customer faster than it costs to
> serve them.** *Falsifier:* per-org gross margin at 10× current scale. If
> serving cost grows with corpus size faster than value does, this is a
> services business wearing infrastructure clothes.

That falsifier is the whole ballgame for which multiple this business
gets, and §13.2 also noted the data to run it was already there ("cost to
serve is knowable"). Nobody had run it. `hub/bench_scaling.py` now does,
and `hub/SCALING.md` records the result.

### 18.1 The measured answer: no path grows linearly with a customer's own corpus

Across a 64× corpus range, fitted exponents in `latency ~ size**alpha`:

| read path | alpha |
|---|---:|
| `search_traces`, selective query | **0.19** |
| `search_traces`, by tag | **-0.00** |
| `entitlements` | 0.58 |
| `fleet_outcomes` | 0.62 |
| `agents_under_management` | 0.68 |
| `list_tags` | 0.74 |
| `search_traces`, query matching every row (worst case) | 0.83 |

A 64× increase in a customer's accumulated history costs **2.2×** on the
read they issue most. On the cost side, link 3's falsifier does not fire,
and the shape is the infrastructure one rather than the services one.

### 18.2 What that does not entitle anyone to say

Three limits, and they matter more than the table.

**It is the cost half only.** The falsifier compares cost growth against
*value* growth. Value per query needs real customers, not synthetic rows —
that is what §17's `fleet_outcomes` and `commons_hits` are for. A clean
cost result removes the cost-side objection to link 3; it does not
establish link 3. Link 3's status moves from "unmeasured" to "half
measured, and the measured half is good."

**It says nothing about concurrency.** Every number is a single query
against an idle database. Cost per customer at N simultaneous customers is
a different measurement that nobody has made, and it is where the
in-process rate limiter (§6 of `hub/DEPLOYMENT.md`) and the connection
pool would actually bind.

**It is one machine's Postgres.** Only the exponents transfer; the
milliseconds are worth nothing to anyone else.

### 18.3 The first run said something different, and the instrument was half the reason

Worth recording, because it is the third time in this document that a
measurement's first answer was about the instrument rather than the world
(§11.1's 10.9% recall, §12.7's ranking correction, now this).

The first run flagged **two** paths as linear-or-worse. They had opposite
causes:

1. **`fleet_outcomes` was genuinely superlinear (alpha 1.12)** — and it
   was three commits old, added by §17. It pulled every matching trace's
   `outcome` JSONB across the wire and counted in Python. Moving the
   counting into one grouped aggregate took 64,000 traces from 572 ms to
   95 ms and the exponent to 0.62. So §17 shipped a real instance of
   precisely the failure mode §13.2 warns about, and §18 caught it — which
   is an argument for running the measurement continuously rather than
   once.

2. **`search_traces` was the benchmark's fault.** Every synthetic row
   shared near-identical title text, so the probe query matched 64,000 of
   64,000 rows. `EXPLAIN` showed a sequential scan feeding a top-N
   heapsort, which is correct for that query — `ORDER BY ts_rank(...)` must
   score every match and no index can serve it. With realistic text
   diversity the same path measures 0.19.

Both numbers are published, not just the flattering one: the worst case is
real, and a fleet that searches for common words will hit it.

### 18.4 What this changes about the case

Narrowly: §13.2's link 3 was the only link with no evidence of any kind,
and the cost half of it now has some. Links 1 (measurable value, one
customer) and 4 (survives platform bundling, argued) are unchanged and
remain the two that need customers rather than code.

That ordering is worth stating plainly, because it is the answer to "what
would make this a large outcome" and it is not a feature list. **Every
remaining question on the critical path needs customers, not
engineering.** Link 1's falsifier is "run `--experiment` on the next two
fleets", link 3's remaining half needs real query volume against real
value, and link 4's is "a customer consolidates onto one platform and
drops this." The instruments for all three now exist and are, as far as
this repository can establish, correct. Nothing further can be learned
about whether this is a billion-dollar business by writing more of it.

---

## 19. Correction to §18.4: the cheapest falsifier could not be run on the product

§18.4 closed with a confident claim, and it was wrong:

> **Every remaining question on the critical path needs customers, not
> engineering.** … Nothing further can be learned about whether this is a
> billion-dollar business by writing more of it.

The first half is still broadly right. The second half was not, and the
gap it hid is the largest one this document has recorded.

### 19.1 What was missing

§11.3 names the moat in one sentence — *"nobody rips out the thing with a
measured effect size on their own data"* — and the measurement it means is
§8's randomized holdout, not §17's before/after comparison. §13.2 goes
further and calls running that holdout **"the cheapest falsifier in the
document and it should be run first."**

`commontrace/experiment.py` implements it correctly and completely. It
works against a **local file store**. The Hub had no notion of a holdout
at all — no assignment, no arms, no observations. Every mention of
`experiment` in `hub/` was a comment or an import of its *statistics*.

So the position was:

- A **Hub customer** — which is to say, the product — could obtain no
  causal number of any kind. `fleet_outcomes` (§17) was the ceiling, and
  it is explicitly observational.
- §11.3's moat sentence was true only of the local tier, which is not
  what anyone is being sold.
- §13.2's cheapest, most gating falsifier **could not be run on paying
  customers** without asking them to abandon the Hub for local files.

That is not "needs customers". That is a missing instrument on the surface
the customers are on, and §18.4 asserted otherwise without checking.

### 19.2 What now exists

`holdout_assign(trace_ids, occasion_id)` and
`record_occasion_outcome(occasion_id, succeeded)` on the MCP surface;
`start-experiment` / `experiment` / `stop-experiment` on the operator CLI;
`HoldoutObservation` as the only structure in the Hub that supports a
causal claim. Analysis is `commontrace.experiment.analyze` unchanged —
imported, for the third time and the third variation on one reason. Here
a drifted copy would randomize the same lesson two ways across a fleet
running both tiers and silently compare two mixtures, biasing every
effect toward zero.

Validated against seeded ground truth, 700 occasions, three lessons:

| lesson | true effect | recovered | 95% CI | verdict |
|---|---:|---:|---|---|
| retry with jittered backoff | +30% | +29.2% | [+21.7%, +36.7%] | HELPS |
| disable the circuit breaker | −25% | −24.5% | [−31.8%, −17.3%] | HURTS |
| always set pool_timeout | 0% | −6.0% | — | no effect (MDE ~13%) |

Every interval covers the true value, and the null case reports its own
power rather than being read as evidence of absence. That validates the
instrument. It says nothing about any real fleet, which is the point of
running it on one.

### 19.3 The one that only this can produce

`HURTS` is not symmetry for its own sake. A lesson retrieved often
*because* it fires on the hardest tasks scores well on every correlational
signal this system has — retrievals, trust, `commons_hits` — and may be
making outcomes worse. No amount of observation separates those two
stories. Withholding it at random does, and nothing else does.

A memory product that cannot detect its own harmful memories is a product
whose corpus degrades silently as it grows, which is the same failure
§16 addressed for the Knowledge Base and had not addressed for a fleet's
own store.

### 19.4 The cost, stated plainly

The withheld fraction gets a worse product on purpose. That is the price
of knowing whether the product works at all; it is bounded by the rate;
and no migration or default ever turns it on. An operator decides, per
org, and the decision is recorded in the audit log.

This is also the honest answer to why a customer would agree: they are
buying the claim in §11.3, and this is the only way anyone — including
them — can check it. A vendor willing to run an experiment that can return
`HURTS` about its own product is making a different kind of claim than one
that reports retrieval counts.

### 19.5 Revised status of §13.2's chain

- **Link 1** (per-org memory delivers measurable value): falsifier was
  *"run `--experiment` on the next two fleets"*, and until now that could
  not be done on the Hub at all. The instrument now exists on the product
  surface. Still needs two fleets.
- **Link 3** (value compounds faster than cost): cost half measured (§18),
  value half still needs real query volume.
- **Links 2, 4, 5**: unchanged.

§18.4's claim, corrected: every remaining question needs customers *and*
the instruments to have been built where those customers are. The second
half was not finished when §18 said it was. It is now, as far as this
repository can establish — and that phrasing is doing real work, because
§18.4 is the second time in two sections that a confident "nothing left to
build" turned out to be a claim nobody had checked.

---

## 20. Update (2026-09-02): the instrument was measuring, and nothing was auditing the instrument

§19.5 closed with "every remaining question needs customers *and* the
instruments to have been built where those customers are", and hedged that
with *as far as this repository can establish* on the grounds that §18.4 had
twice declared "nothing left to build" without checking. This is the third
time, and the hedge earned its keep.

The instruments were built. What was never built is the thing that decides
whether an instrument's reading means anything.

### 20.1 The defect, stated as the number it produces

`commontrace/experiment.py` is right. Two-proportion tests, a 95% interval,
Benjamini-Hochberg across lessons, underpowered comparisons kept out of the
correction so they cannot inflate *m*, and an explicit `UNDERPOWERED`
verdict so a small sample never reads as "no effect". Nothing in the
arithmetic needed fixing.

But `analyze()` only ever sees occasions that HAVE a recorded outcome, and
both tiers dropped the rest before it. `hub/crud.py:causal_effects` did it
in SQL — `succeeded IS NOT NULL` — so nothing downstream could even count
what went missing, let alone which arm it came from. `hub/models.py` states
the reasoning, and it is correct:

> An observation with no outcome is excluded from the analysis rather than
> counted as a failure: an agent that crashed before reporting is missing
> data, and scoring it as a loss would bias the arm that crashed more.

Excluding is the right handling. It is unbiased **only if both arms lose
outcomes at the same rate**, and nothing anywhere checked that. Worse, there
is a specific reason to expect they do not: the withheld arm is *by
construction* the arm working without its memory, so it is the arm more
likely to run long, escalate, or be abandoned before anyone writes up how it
went. The treatment effect leaks into who gets measured.

Reproduced, in `tests/test_integrity.py`. A fleet of 600 occasions where the
lesson does **nothing** — both arms succeed at exactly 50% — and the only
asymmetry is that a withheld occasion which failed often never gets
reported:

| | |
|---|---|
| True effect | **0.0%** |
| `analyze()` reported | **HURTS, −12.6%** |
| 95% CI | **[−20.8%, −4.4%]** — does not contain zero |
| p | **0.003** |
| Verdict | significant, adequately powered |

Driven end to end through `commontrace experiment` on a seeded store, the
same defect reads `HURTS −17%, 95% CI [−28%, −7%], p=0.002`.

The failure mode is the dangerous kind. It does not error. It does not
return empty. It does not read as underpowered. It reads as a clean,
significant, well-powered result with a plausible effect size and a tight
interval — and it points the wrong way about a lesson that was fine.

### 20.2 Why this is a strategy problem and not a bug report

§13.3 says one thing makes this more than a good DevTools business: *a
product that retrieves the right prior experience reliably, and can prove
causally on the customer's own data that doing so changed the outcome.*

That claim is only worth what it survives. The first question a sophisticated
buyer's data-science function asks — the first question a technical diligence
partner asks — is not "what was the p-value". It is **"how do you know that
number isn't an artifact of who got measured?"** Until now the honest answer
was "we don't check", and the product would have answered it with a
`HURTS` verdict about a lesson that does nothing.

§13.3 also names the three things a competitor bolting memory onto an
existing product lacks: the activation-condition data model, the
occasion-level join, and *a willingness to publish nulls*. The third was a
disposition. It is now mechanical: a run whose sample cannot support an
estimate does not get a hedged number, it gets a refusal to report one.

### 20.3 What was built

`commontrace/integrity.py`, shared by both tiers — the same discipline
`holdout_io.py` applies to arm assignment, for the same reason. Five checks,
each with a severity that means something specific (`INVALIDATES` — a named
mechanism is biasing the estimate; `WEAKENS` — the sample is degraded but
not demonstrably biased; `OK` — checked, nothing found, stated explicitly so
silence is never mistaken for a clean bill):

1. **Differential attrition.** The load-bearing one. Reports the direction,
   because which arm loses data decides which way the number is wrong.
2. **Arm balance.** Realized withheld share against the configured rate.
   Assignment is a deterministic hash, so a large gap is not luck.
3. **Mid-run re-randomization.** A changed salt or rate re-randomizes every
   occasion, so the log stops being one experiment and becomes two pooled —
   and an occasion can sit in opposite arms in each.
4. **Conflicting arms.** One (lesson, occasion) recorded in both arms:
   evidence for and against the same lesson at once.
5. **Outcome variation.** An all-succeeded corpus yields a difference of
   exactly zero with a tidy interval, and reads as a confident null.

Plus a **power projection**: how far each lesson is from being answerable
and, where the log is dated, roughly when. That is not a validity check, it
is what decides whether a pilot lands. `experiment` already said
`UNDERPOWERED`; a team told that on day 30 has spent the pilot, and the same
team told on day 3 that the control arm lands in 94 days can raise the
holdout rate that afternoon. The control arm almost always binds and the
reason is arithmetic: at a 10% holdout it takes ~100 occasions to put 10 in
the control, so a run reaches an answer about ten times slower than its
occasion count suggests. The projection now says so, and says what rate
would fix it.

Wired everywhere the number is read: `commontrace experiment` (validity
above the table, and `--strict` fails a compromised run — the flag means
"stop if the memory is hurting", and a biased comparison cannot answer that
either way), `commontrace pilot` (a compromised run yields no effects rather
than effects with a caveat elsewhere in a renewal deck), `prove outcomes`,
the Hub's `fleet_outcomes` response under `causal.integrity`, and the local
MCP server's `experiment_status`.

### 20.4 What it deliberately does not do

**It does not correct the estimate.** A compromised experiment does not get
a fixed number here. It gets a report saying the number should not be read
and why, which is the honest output and the only one available: nothing can
recover an outcome that was never recorded.

**It cannot detect contamination.** An agent that uses a lesson it was told
to withhold leaves no trace in the record, and biases the effect toward
zero. No analysis can find it. It is honoured by the client or not at all,
which is why it is stated in the tool descriptions, the skill, and every
rendered report rather than checked — silence about it would read as
coverage of a failure nothing here can see.

**Absence of a finding is not proof of validity.** These detect the failures
that leave a trace in the assignment log. That set is not everything.

### 20.5 What this changes about §13.2's chain

Nothing about which links are open, and that is the point — this does not
move a link, it makes one of them *checkable by someone who does not trust
us*.

- **Link 1** (per-org memory delivers measurable value): unchanged, still
  needs two fleets. What changed is that when those two fleets report a
  number, there is now something that says whether the number is an estimate
  of anything. Before this, link 1's falsifier could have returned a
  confident false answer in either direction and nobody would have known.
- **Links 2, 3, 4, 5**: unchanged.

The correction to §19.5 is narrow and worth stating plainly: *the
instruments existed; nothing audited the instruments.* An instrument nobody
audits is not a measurement, it is a number — and this document has now
found three separate confident claims that "nothing is left to build",
each of which was a claim nobody had checked. That rate is itself the
finding. The rule that follows from it is not "check harder next time"; it
is that a claim of completeness is worth exactly as much as the falsifier
attached to it, and this section's is `tests/test_integrity.py` — a fleet
where the truth is known to be zero, which the product must refuse to score.

---

## 21. Update (2026-09-02): §20 audited the sample and left the treatment unpinned

§20 shipped a validity layer and said, of the checks it did not include,
that "absence of a finding is not proof of validity: these detect the
failures that leave a trace in the assignment log." That sentence was
written as a caveat about *contamination*. It was also, unnoticed, true of
something much more ordinary.

§20 checked whether the **sample** could support an estimate. Nothing
checked whether the **treatment held still**.

### 21.1 The defect

A lesson is a file. `lesson approve`, a text editor, and — since the MCP
surface shipped — an agent calling `draft_lesson`, all rewrite it in place.
The holdout log recorded the lesson by **slug**: a mutable name.

So editing a lesson on day 10 of a 30-day run means occasions 1–200 were
treated with one instruction and 201–400 with another, `analyze()` pools
them into a single arm, and the reported effect is for a treatment that is
an average of two — one of which no longer exists anywhere. And a concluded
run reporting "+12%, p=0.01" describes text that the next edit silently
deletes, with no record of what it was.

This is precisely the defect `Organization.holdout_salt` was built to make
detectable, one level down. There the **randomization** could change
silently. Here the **thing being randomized** could. §20 added a check for
the first and did not notice it had described the second.

The Hub had it identically on a different object: its holdout randomizes
traces, and `amend_trace` rewrites title, context and solution in place.

Worth stating plainly, because it is the uncomfortable part: **the MCP work
two commits earlier increased the exposure.** Before it, rewriting a lesson
mid-experiment took a person opening a file. After it, an agent could do it
unattended, as an ordinary and *correct* part of curating. A feature that
makes the product better made a measurement defect easier to trigger, and
nothing connected the two at the time.

### 21.2 Why this is the system-of-record question, not a bug

§13.3's claim is a causal number on the customer's own data. §20 made that
number auditable *as a statistic*. This makes it auditable *as a record*:
what was measured, what it said, who changed it, when.

That distinction is the whole difference between a tool and a system of
record, and systems of record do not get bundled away. A memory layer that
stores lessons is a feature any agent platform can add in a quarter. A
memory layer that can answer **"what instruction was this fleet following on
March 4th, who approved it, and what did withholding it do"** is a different
kind of object, and the answer has to be reconstructible from the record
rather than from someone's memory of it.

Every ingredient for that already existed here — approval, audit, standing,
reliability, causal effect. The one missing piece was that none of it was
pinned to a *version* of the thing it was about.

### 21.3 What it deliberately does not do

**Nothing is backfilled, on either tier.** What a lesson said at assignment
time is unrecoverable once it has been edited. A run with no recorded
revisions is therefore reported as *unchecked* — WEAKENS, not OK and not
COMPROMISED. Both wrong answers were available and both were rejected:
stamping today's digest on old rows would assert the treatment was stable on
exactly the runs where nobody can know, and reporting them clean would let
an unversioned log read as a stable treatment, which is the state this
exists to distinguish.

**The digest covers what an agent reads, and nothing else.** `uses` and
`last_hit` move on every retrieval; hashing them would flag every experiment
within a week, and a validity report that cries wolf is worse than none —
the same reasoning that keeps `check_arm_balance` quiet below 30
assignments.

### 21.4 What this changes about §13.2's chain

Again nothing about which links are open, and again that is the point. Links
1–5 are unchanged and every one of them still needs customers.

What changed is the same thing §20 changed, one layer down: when those
customers produce a number, there is now a record of what the number was
about. §20 made the estimate checkable by someone who does not trust us.
This makes it *reconstructible* by them — and the second is the harder
property, because it survives the people who ran the experiment leaving.

### 21.5 The pattern, now four for four

§18.4, §19.5, §20 and now §21 are four consecutive sections in which a
confident statement about what was left to build turned out to be a claim
nobody had checked. The specific claims differed; the shape did not.

§20 drew the rule: *a claim of completeness is worth exactly as much as the
falsifier attached to it.* This section is evidence that the rule works and
that applying it once is not enough — §20's own caveat contained the next
defect, correctly worded, and it still took a separate deliberate look to
find it. The corollary is narrower and more useful than "check harder":
**the caveats are where the next defect is.** They are the places someone
already knew the ground was soft and wrote it down instead of digging. This
document should be read that way, starting with §21.3.

### 21.6 Postscript: §21.5's rule caught its own defect within the hour

§21.5 said the caveats are where the next defect is. The first place to look
was §20's own tuning decision, and it was wrong.

`check_arm_balance` shipped sharing the attrition check's alpha of 0.10.
Measured afterwards, a **correct** randomizer trips a two-sided test at that
alpha about 10% of the time — at every sample size, because that is what an
alpha is. The check runs on every experiment, so roughly one sound run in
ten would have been reported COMPROMISED for nothing.

§20's own commentary named the failure this creates ("a validity report
whose findings are mostly noise teaches people to skip the section where the
real ones appear") and then built it, because the reasoning that produced
the loose alpha — *for a validity check a false negative is the expensive
error* — is correct for attrition and wrong for arm balance. Attrition is a
gradient; a ten-point reporting gap is a real finding. Arm balance is
binary: a deterministic hash is being applied or it is not, and a broken
assigner misses by many standard deviations. One number for two checks was
the mistake.

At 0.001 the false-positive rate is ~0.1% and every realistic breakage is
still caught; both are measured in the test suite rather than argued.

It was found by a test that failed about one run in fifteen under random
ordering — which is worth recording as the cheapest instrument in this
document. Nobody reasoned their way to it. A flaky test did.

---

## 22. Update (2026-09-02): the claim, end to end, and what it cost to make it true

§21.5 said the caveats are where the next defect is. Applied five more times
in one session, it found five, each in the load-bearing claim rather than
around it. Recorded here because the *pattern* is the finding, not any one
of them.

### 22.1 What was wrong, in the order it was found

**The AI-first half of the product could not use its own memory.** Every
local-tier step — retrieve, capture, distill, approve — was an argparse
command, so the only agents that could use their own store were the ones
with a shell. A support agent in a helpdesk could talk to a remote Hub and
do nothing else. `commontrace serve` (MCP over stdio) closed it.

**The causal number could be confidently wrong.** `analyze()` sees only
occasions that got an outcome, and both tiers dropped the rest before it.
That is unbiased only if both arms lose outcomes at the same rate, and the
withheld arm — by construction the one working without its memory — is the
one that runs long and gets abandoned. Reproduced: a fleet where the lesson
does nothing, reported as **HURTS, −12.6%, p=0.003**, significant and
adequately powered.

**An effect size was attached to a mutable name.** A lesson is a file; the
holdout log recorded its slug. Edit it mid-run and the two halves of the
experiment are different treatments pooled into one arm. The MCP work two
commits earlier had made this *easier* to trigger, and nothing connected the
two at the time.

**"No measurable effect" was reported by designs that could see nothing.**
The gate was `min_arm = 10`, at which the minimum detectable effect is 61
percentage points. A full 240-occasion pilot with a real +25pp effect came
back `NO_MEASURABLE_EFFECT`. The report printed the MDE beside the verdict,
which is not enough — the verdict is what travels.

**The rate the planner recommended could not be set.** The holdout rate was
a CLI flag default on one surface and a hardcoded constant on the other, so
an agent-driven fleet could not change it at all — and a fleet using both
interfaces pooled two randomizations without doing anything wrong.

### 22.2 The claim, demonstrated rather than argued

Sized by the product (`--plan`: 900 occasions cannot answer a 15-point effect
at 10%; use 19% or more), configured to 25%, then curated and run **entirely
over MCP by an agent with no shell** — 900 occasions against a seeded +18pp
effect:

| | |
|---|---|
| Validity | **Sound** — 900 of 900 assignments observed |
| Verdict | **HELPS** |
| Effect | **+14%**, 95% CI **[+7%, +22%]**, p<0.001 |
| Revision under test | `42d099b46770` |

The interval covers the true value. And the mirror image, from the same
session's test suite: a fleet where the memory does nothing, which the
product **refuses to score** rather than reporting the −12.6% its own
arithmetic produces.

Both halves matter, and the second is the one that is hard to copy. A vendor
willing to run an experiment that can return `HURTS` about its own product
is making a different kind of claim; a vendor whose product *declines to
report a number it cannot stand behind* is making a stronger one.

### 22.3 What this does and does not change about §13.2

Links 1–5 are unchanged. Every one of them still needs customers, and no
amount of further engineering moves them.

What changed is what a customer's number will be worth when they produce it.
Before this session the falsifier §13.2 calls cheapest and most gating could
have returned a confident wrong answer in either direction — from
differential attrition, from a lesson edited mid-run, or from a design that
could not detect anything — and nobody would have known. It can still return
"we cannot tell yet". It can no longer return a false answer quietly.

The customer console (`/app`) exists for the same reason: the person who
decides on renewal does not run `commontrace prove outcomes`, and a claim
nobody in the buying organisation can see is not doing the job it was built
for.

### 22.4 The rule, sixth time

A claim of completeness is worth exactly as much as the falsifier attached to
it, and **the caveats are where the next defect is** — they mark the places
someone already knew the ground was soft and wrote it down instead of
digging.

The cheapest instrument in this document remains a test that failed one run
in fifteen under random ordering. Nobody reasoned their way to the
arm-balance defect. A flaky test did.
