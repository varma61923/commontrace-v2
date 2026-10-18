# Guarantees, estimates and deployment assumptions

Every number below is conditional on the declared design. Canonical
`experiment.analyze` and `integrity.audit` remain the evidence of record. A
signature proves an authorized signer committed bytes; it does not prove those
bytes are true or that a model executed the claimed procedure.

| Mechanism | Claim and conditions | Practical limit |
|---|---|---|
| Lineage | Authenticated input hashes, minimum integrity, bounded ancestor/descendant traversal and cascading review flags | Origin independence requires operator identity governance; unattested legacy data is unknown |
| Merkle transparency | Inclusion and prefix consistency relative to a pinned signed tree head | A complete hidden history needs an external witness/checkpoint to detect; HMAC is a shared-secret MAC |
| E-process | Nonnegative predictable betting evidence under its conditional null; e-BH handles arbitrary shared-task dependence for positive (HELPS) claims at a fixed family/joint stopping rule, while harm withdrawal keeps its marginal anytime trigger | Exact propensities, binary bounded outcomes, valid missingness and interference assumptions are required; never multiply overlapping snapshots |
| Conformal harm gate | CRC expected marginal conservative injected-failure risk under exchangeability, calibrated on ungated single-memory randomized occasions and bound to their scorer scale; LTT bounds a fixed threshold/metric family | Parallel holdouts cannot identify individual harm. Failure is an upper-bound proxy; covers discretionary memories (core and hazards are outside it) and, in recall, the lessons channel only; drift/online updates are heuristic, not a realized-fleet harm guarantee |
| Promotion | Finite-sample anytime confidence sequence for the supported frozen policy comparison; a Thompson gate certificate binds the gate's configuration, not its per-era rates | Preregistered criteria, fixed training split, exact propensities above a declared floor, bounded outcomes, stationary traffic and no interference |
| Cluster experiment | Time-uniform bounded HT interval for equal-cluster treatment effects | Independent randomized clusters, no cross-cluster interference; switchbacks additionally require sufficient washout |
| Gold-label correction | Unbiased finite-population prediction-plus-HT-residual estimate | Exact independent Bernoulli gold inclusion probabilities, bounded predictions/outcomes; treatment-effect intervals also include assignment uncertainty |
| Uplift | Conservative fixed-look held-out top-half RATE and simultaneous class bounds | Fixed ranking/training split, pre-treatment covariates and exact propensities; repeated model selection needs separate validation |
| Factorial valuation | Unbiased main/pairwise randomized contrasts with fixed-family Hoeffding intervals | Complete independent Bernoulli vectors, bounded outcomes, independent tasks; Data Banzhaf equivalence is not claimed |
| Subject-key destruction | Previously encrypted envelopes become unreadable after their independent subject key is destroyed | Excludes preexisting plaintext/key backups, remote systems, hardware copies and machine-learning unlearning; probe success is heuristic |
| Scoped retrieval / IFC | Owned visibility is checked before recovery/spatial ranking; low-integrity material is returned as quoted data | Trusted caller authentication must establish scope; labels are an application contract, not an LLM or process sandbox |
| Incident evidence | Signed frozen state, receipt replay, legal holds and mapped evidence fields | Not regulatory certification, legal advice, or proof of what an uninstrumented process actually consumed |
| Physical safety | Registered hazards bypass holdouts and covered compression/eviction paths; durable signed decisions and explicit retention dates | No robot motion-safety guarantee; trusted marking, fresh sensors, interlocks and scoped application integration are necessary |
| Counterfactual attribution | Budgeted sampled replay diagnostics and sparse surrogate contributions | Replay/model/fit error can dominate; diagnostics cannot create canonical release evidence |
| Clean-room privacy | Clipped fixed-size mean with Laplace replacement-adjacency DP and basic per-querier composition | Trusted identities/cohort size and coordinated accounts are needed; sandbox/enclave execution is external |
| Compilation | Source-bound manifests, live stale checks and exact-artifact F2 certificate requirement | The CPU trainer is governance reference metadata; production training and model-process enforcement are plugins |
| Edge cache | Signed update authentication, inclusion and continuity from the last witnessed head; hazards survive refreshes and journal compaction | Trusted first-head provisioning and freshness policy are necessary; offline context can become stale |

Learned utility retention, discrepancy weights, empirical-Bayes allocation,
knapsack value models, surrogate agreement, voting stability, source reputation,
cache-price/latency estimates and poisoning alarms are explicitly estimates or
heuristics. None establishes regulatory compliance, universal deletion,
semantic truth, insurance coverage or safe robot actuation.

UniMem [pooled evidence](pooled-evidence.md) reports model-based random-effects
estimates, not a new promotion guarantee. Its combined e-value tests a joint
directional null; independent products require independent ownership groups.
Operator-established ownership prevents account splitting from adding studies.
Gaussian calibration assumes fixed public counts, frozen public nuisances and
one bounded-score replacement; finite-precision sampling is a reference, not an
audited exact-arithmetic DP implementation. Few, highly unequal studies can
under-cover, and the interval must not authorize a release.

The UniMem [skill foundry](skill-foundry.md) reuses canonical promotion evidence;
template induction and lexical screens remain heuristics. Catalog guarantees
require callers to deliver the logged arm, complete outcomes, exact propensities,
stationary traffic and no interference. Conformal harm bounds additionally
require exchangeability with the frozen pilot calibration. A shared bundle-size
factor fixes eligibility before individual arms. Post-publication decay testing
uses a fixed future-data split and a conservative prior effect bound. Managed
exports recheck source scope, revocation and expiry; erasure covers local managed
content, with distributed copies and backups explicitly outside that boundary.

UniMem [RL exports](rl-export.md) require complete authenticated cohorts and
match preference pairs within full labels and observed tasks. DR advantages
estimate the logged policy mixture's improvement, not an individual effect.
Sampled token KL and confidence-based replay remain estimates and heuristics.
Managed erasure invalidates manifests and content; downstream training and
already delivered bytes require acknowledgement and possible retraining.

UniMem [writer trials](writer-holdouts.md) randomize the parent availability of
authenticated non-hazard contributions. Masked memories are ineligible, so
inner effects condition on parent eligibility. Coverage requires complete
outcomes, exact propensities, consistent delivery, stable traffic and no
interference across occasions. Reliability and MAST tags remain estimates and
heuristics; they do not upgrade authority or certify semantic truth.

UniMem [contagion diagnostics](contagion.md) cover the admitted, instrumented
dependency graph within explicit node, depth and retention bounds. Truncation
and missing instrumentation limit completeness. Lineage-root `n_eff` measures
structural diversity; it does not establish independent errors or semantic
truth. Re-retrieval always repeats canonical admission.

UniMem [memory telemetry](otel.md) performs source admission before attributes
and omits invisible records. Content requires explicit opt-in. SDK span creation
does not acknowledge delivery to an exporter; optional telemetry failures do
not change committed operations. Outcome slices are descriptive and retain
canonical arms and propensities, without issuing causal release evidence.

UniMem [change streams](change-streams.md) re-admit current sources before
payload construction and deferred egress. Durable canonical outboxes recover
committed changes idempotently; cursors bind subscriptions, while sequence
positions expose activity within a root. Conditional byte receipts require
correct operator-supplied original fingerprints, a complete bounded local
inventory and all tracked downstream erasure ACKs. Unknown encodings, opaque
envelopes and uninspected remote stores remain unverified. These observations
exclude backups, hardware remanence, RAM and universal model unlearning.

UniMem [payload tiers](tiering.md) enforce configured regions and source scopes,
verify copies before withdrawal, refuse hibernation of protected source lineages
and purge managed cold versions on erasure. Region placement and remote version
lifecycle remain operator responsibilities. Lifecycle hooks remove cold latency
only when they complete before the turn. Native filters narrow candidates before
final admission; approximate-index recall equality is not guaranteed. Embedding
cutover uses a held-out recall estimate and an atomic rollback pointer.

Delegated capabilities narrow primary identity and scope. Local parent-holder
signatures prevent prefix stripping; shared PostgreSQL nonces enforce Hub replay
rejection. Runtime provenance publishes from committed metadata outboxes. Handoffs
recheck recipient clearance and current sources and impose sticky labels. A
revoked chain quarantines authenticated descendants; release needs an independent
current-content authorization. Quarantine does not certify byte erasure.
Scoped stores, trusted operator pins/host/clock and instrumented dependency bounds
apply; see [delegation limits](capabilities.md).

Validation includes seeded tests for predictable betting, conformal conservatism,
promotion, write-back contamination, detector debiasing, factorial interactions,
uplift rejection/power and culprit recovery. Adversarial tests cover tampering,
forgery, cross-owner reads, lineage laundering, over-budget queries and unsigned
or forked edge updates. See [development](../README.md#development) for
validation commands and CI results; optional vendor/cloud/ROS hardware
deployments are not exercised by CI.

UniMem [team isolation](team-isolation.md) validates declared point-read versions
inside canonical writer transactions. Snapshot mode detects write/write conflicts;
serializable mode requires every declared point read to remain current; sequenced
mode also requires an unexpired work lease with the current durable fence. The
guarantee requires a complete point-read set, a single shared local lock domain
and all writers using the protocol. It excludes predicate isolation and distributed
consensus. Scoped conflict details and fetch-by-ID reapply admission. Semantic
merges retain sources for review; owner-authorized privacy purges cannot add or
edit facts and need no work lease. Defaults preserve existing unconfigured stores.

UniMem [sentinel](sentinel.md) uses prospective, content-pinned segment families
and summable alpha spending for transfer quarantine.

UniMem [personalization audit](personalization-audit.md) separates agreement,
accuracy and task outcome using simultaneous fixed-horizon intervals and exact
propensities. A flattering flag requires preregistered practical equivalence;
non-significance is insufficient. External grading independence remains an
assumption, and a class result is not individual-member accuracy evidence.

The sentinel's intersection-union e-values
and dependent-safe e-BH require conditional directional nulls, exact propensities,
complete canonical outcomes and a common stopping time. Non-rejection is not
evidence of transfer safety. Update release additionally requires supported
factorial canary non-regression bounds; their true score bound includes both
propensities and their parameter range is `[-2,2]`. Deployment adapters must honor
the returned arms. Drift causes and sampled token KL remain explicitly limited.

UniMem [optimizer intake](optimizer-intake.md) treats proposed edits as hypotheses;
claimed validation scores never become release evidence. Typed assignments,
complete outcomes and the canonical promotion certificate remain necessary.
Catalog delivery, exact propensities, stationary traffic, no interference and
fixed pre-treatment segments are assumptions. Harm eligibility additionally
requires matched frozen calibration. Per-segment Pareto membership is an estimate,
not an optimality guarantee. Vendor serialization fixtures verify their documented
subsets, not native SDK execution. Source changes, scope restrictions and erasure
withdraw candidate delivery; delegated reads require the artifact namespace/class.

## Finite policy change proofs

The supported closed policy language exhaustively compares up to 65,536 old/new union request tuples, including principal attributes and domain expansion. No-widening is sound and complete only for that finite domain and trusted enforcement metadata. Widening activation requires an exact, unexpired operator/admin review and current generation. Native Cedar validation is real and optional, additionally bounded to 8,192 requests. Remote relationship state, arbitrary external retrieval callbacks and compromised operators are excluded. See [memory policy](memory-policy.md).

## Owned passports and selectively disclosed credentials

Passports bind all entries and producer metadata with real Merkle commitments and Ed25519 signatures. Receiving key pins are explicit; all imports remain in review. SD-JWT memory credentials use real native signatures, salted disclosures, holder binding and single-use verifier nonces. Local revocation uses current source/registry state; foreign status quotes must bind the verifier challenge. These authenticate issuer assertions, not their truth or model execution. Canonical erasure blocks new exports and purges managed artifacts; delivered remote plaintext remains conditional on downstream deletion and acknowledgements. The profile and bounds are documented in [memory passports](memory-passport.md).

Purpose-bound consent intersects existing admission with an authenticated request
purpose and explicit processing operations. Expired consent denies reads before
cleanup; [retention evidence](purpose-consent.md) distinguishes local purge,
actual envelope-key destruction and outstanding remote acknowledgements.

## Attestation, household scopes and model KV

[Attested key release](attested-key-release.md) returns only an X25519-encrypted
tenant key after a fresh quote matches a tenant-logged measurement and its exact
worker key, payload, operation and nonce. Release requires shared PostgreSQL
challenge state and a configured tenant KMS envelope. Hardware appraisal is
only as strong as the pinned AMD/Intel/NVIDIA verifier configuration; the
simulated backend is test-only, and actual zkVM proving is separately marked
unverified when the native toolchain is absent.

[Household scene persistence](household-memory.md) requires identity signals
from the configured robot resolver and active consent for every recognized
person. Unknown or unconsented scenes are ephemeral. No-memory room policy is
checked before scene and spatial persistence. The callback must be wired into
the ROS scene path; bypassing it is outside the guarantee. Local proxy records
include an authority reference but do not validate legal capacity or the
authority document.

[KV capsules](kv-capsules.md) use an HMAC key derived from the store's identity
secret, source content digest and exact model fingerprint. Serving rechecks
canonical source admission and compressed-byte integrity. Erasure attempts
deletion at all configured tiers and lists outcomes in its receipt. A failed
external deletion remains an operator obligation; the core cannot certify a
vendor's purge. Promotion score is a policy input, not a statistical certificate.

[World-model trust](world-model-trust.md) binds outcomes to an immutable model
digest, embodiment and task, and computes an anytime confidence sequence from
unique real episode outcomes. Perturbed-action checks reject a registered model
that ignores the tested action. A failed simulation may veto; passing simulation
can only become eligible for a real trial after the configured trust threshold.
No simulation result promotes a memory. The guarantees assume the registered
digest matches the actual evaluator, outcome labels are accurate, trial outcomes
are complete, and the adapter reports honest rollouts. A changed model or
embodiment starts a distinct ledger with no inherited evidence.

[Proprioceptive signatures](robot-signatures.md) use empirical per-channel
quantile bands conditioned on task phase, payload class, firmware, skill,
embodiment and site. A flagged deviation is an anomaly estimate, not a diagnosis
or certified false-alarm rate; the stated tail fraction is estimated from the
baseline sample. Firmware drift is an event-time comparison with a separate
record time. The forecast adapter is optional and no published robotics
validity is claimed for time-series foundation models on joint data.

[Oblivious recall](oblivious-recall.md) uses the two-server XOR-PIR reference:
one server's uniformly randomized bitmap share reveals no selected block index,
provided the replicas do not collude and the database ordering is identical.
Label ceilings are applied before fixed-capacity cluster padding. The observable
cluster set/count and ordinary network metadata remain leakage; transport,
replica collusion, and truth of public centroids are deployment assumptions.

[Robot working memory](robot-working-memory.md) is bounded local state with
no network request in retrieval; latency is measured per host, and explicit
consolidation links keyframes to an audit record. It is not a real-time operating
system guarantee. [Robot site memory](site-memory.md) preserves peer signatures,
causal conflicts, and safety removals in its local reducer; Open-RMF mapping and
Automerge compatibility remain adapter/version limited.

[Safety cases](safety-case.md) bind claims and evidence to supplied artifact
digests, mark dependency-linked evidence stale, and report weak or missing hazard
coverage. Supplied evidence truth is not independently attested, and the graph is
not a UL 4600 assessment. [The STL monitor](robot-shield.md) computes bounded
sampled robustness, records proposed-action digests and memory lineage, and
returns hold for missing provenance, violations, or exhausted work budget. The
robot integration must honor that verdict; this is not a certified safety
function and never replaces ISO 10218 controls. [Vertical pack demos](vertical-packs.md)
are deterministic seeded synthetic experiments; their intervals estimate only
the generated sample and do not establish production impact.
