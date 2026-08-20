# The fork in the road

An engineering-leadership read on what CommonTrace is, what it is being
sold as, and the one number that decides which company this becomes.
Opinionated on purpose. The business calls at the end are not mine to make.

---

## 1. The gap between the deck and the repository

The pitch deck's third panel is *"Lessons compound into intelligence across
the fleet"*, drawn as a **Shared Intelligence Hub** every agent connects
to, with **"strategic moat"** as the terminal business outcome. The moat
argument is a network-effects argument: one org's mistake improves every
other org's agents.

The repository implements something different, and does it well:

| The deck sells | The code does |
|---|---|
| Cross-org collective intelligence | Strictly per-org memory. Every read path in `hub/crud.py` is unconditionally scoped to the caller's own `org_id`. |
| A commons that compounds across companies | `Trace.shared_with_commons` exists as a column and **is never read by a single query.** |

That is not an accident or an oversight — it was the correct conservative
call (see `hub/README.md`, "Tenant isolation vs. the cross-org commons
pitch"). Walls first, doors deliberately. But it means the thing the moat
rests on has not been built, and — more importantly — **has never been
measured.**

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

Everything above is a hypothesis. The entire (B) thesis reduces to one
empirical quantity:

> **Of the failures a new fleet is about to hit, what fraction has some
> other fleet already solved?**

If that number is 40%, the commons is worth building and the network effect
is real. If it is 4%, (B) is a mirage and the honest move is to sell (A)
extremely well.

Nobody knows this number. It is unmeasured, it is cheap to measure, and it
is being treated as an assumption. That is the actual bottleneck — not
features, not scale, not the container image.

Note the benchmark already contains the *within-org* version of exactly
this question: `transfer_gap` measures whether a lesson learned on project X
helps on project Y (`benchmark/STATUS.md` §2.4). It was mechanically 0% for
a long time because everything was tagged one project. The cross-*org*
version is the same measurement one level up.

## 6. What I built, and why it is the highest-leverage thing available

`commontrace overlap` — a **Fleet Overlap Report**.

It answers "how much would fleet B gain from fleet A's lessons?" **without
either fleet sending the other any lesson text**, by exchanging MinHash
signatures instead of content.

Why this specific thing:

1. **It de-risks the company's biggest decision for near-zero cost.** You
   get the (A)-vs-(B) number before betting the roadmap on it.
2. **The output is itself the strongest sales asset you have.** "We analyzed
   your traces against the commons: 47 of your recurring failures are
   already solved, here are the three costing you the most" is a far better
   first meeting than any deck slide.
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

The deck's own headline is *"Raw memory remembers. CommonTrace
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
compounding loop the deck's final panel promises, and it is a data
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
- Two lessons that *always* co-fire cannot be told apart empirically — they
  share every outcome. That is the hard case and it needs either an
  intervention (withhold one and compare) or human judgment.
- Nothing feeds these scores back into retrieval ranking yet. Doing so is
  the step that makes the loop actually close, and it should wait until the
  scores have been validated against a real corpus.

## 8. Decisions I cannot make for you

Flagging rather than inventing:

1. **(A) or (B)?** Run the overlap report on 2–3 real customer stores
   first. Let the number decide; do not decide before the number.
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

## 9. What I would not do

- **Do not build the cross-org read path yet.** It is the one change that
  can leak a customer's data across a tenant boundary, and today's strict
  isolation is an asset in every security review. Build it after the number
  justifies it and the terms in §8.2 are settled — not before.
- **Do not lead with the benchmark numbers.** −53% and 9%→78% are real and
  externally confirmed, and they are per-org results. Citing them as
  evidence for the *commons* would be claiming something they do not show.
