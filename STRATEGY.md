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
