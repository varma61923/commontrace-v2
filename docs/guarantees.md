# Guarantees, estimates and deployment assumptions

Every number below is conditional on the declared design. Canonical
`experiment.analyze` and `integrity.audit` remain the evidence of record. A
signature proves an authorized signer committed bytes; it does not prove those
bytes are true or that a model executed the claimed procedure.

| Mechanism | Claim and conditions | Practical limit |
|---|---|---|
| Lineage | Authenticated input hashes, minimum integrity, bounded ancestor/descendant traversal and cascading review flags | Origin independence requires operator identity governance; unattested legacy data is unknown |
| Merkle transparency | Inclusion and prefix consistency relative to a pinned signed tree head | A complete hidden history needs an external witness/checkpoint to detect; HMAC is a shared-secret MAC |
| E-process | Nonnegative predictable betting evidence under its conditional null; e-BH handles arbitrary shared-task dependence at a fixed family/joint stopping rule | Exact propensities, binary bounded outcomes, valid missingness and interference assumptions are required; never multiply overlapping snapshots |
| Conformal harm gate | CRC expected marginal conservative injected-failure risk under exchangeability; LTT bounds a fixed threshold/metric family | Parallel holdouts cannot identify individual harm. Failure is an upper-bound proxy; drift/online updates are heuristic, not a realized-fleet harm guarantee |
| Promotion | Finite-sample anytime confidence sequence for the supported frozen policy comparison | Preregistered criteria, fixed training split, exact propensities above a declared floor, bounded outcomes, stationary traffic and no interference |
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
| Edge cache | Signed update authentication, inclusion and continuity from the last witnessed head | Trusted first-head provisioning and freshness policy are necessary; offline context can become stale |

Learned utility retention, discrepancy weights, empirical-Bayes allocation,
knapsack value models, surrogate agreement, voting stability, source reputation,
cache-price/latency estimates and poisoning alarms are explicitly estimates or
heuristics. None establishes regulatory compliance, universal deletion,
semantic truth, insurance coverage or safe robot actuation.

Validation includes seeded tests for predictable betting, conformal conservatism,
promotion, write-back contamination, detector debiasing, factorial interactions,
uplift rejection/power and culprit recovery. Adversarial tests cover tampering,
forgery, cross-owner reads, lineage laundering, over-budget queries and unsigned
or forked edge updates. See the PR validation record for actual commands and
results; optional vendor/cloud/ROS hardware deployments are not exercised by CI.
