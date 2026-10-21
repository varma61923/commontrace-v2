# UniMem cross-tenant pooled evidence

Use pooling when independently operated tenants evaluate the same public skill,
writer policy or memory class and each has too little traffic for a useful
interval. Units match by a public SHA256 content fingerprint; private memory IDs
and episode text never enter contributions. This is an **estimate**, with no
promotion certificate or permission to bypass a tenant's release gate.

The reference combines AIPW score sums, squared sums and counts using
DerSimonian–Laird random effects and a modified Hartung–Knapp Student-t interval.
It reports between-owner variance, leave-one-owner-out influence, design power
at the pooled estimate and an approximate 80%-power minimum detectable effect.
Within-owner shards merge before fitting. Operator-pinned ownership and positive
stake follow the clean-room trust boundary: account creation cannot establish
independence. Operators must investigate common control and overlapping cohorts.
Repeated releases must describe disjoint observations; a signature cannot prove
that a contributor supplied truthful, disjoint data.

## Assumptions and limits

Groups must be independent, the trial must have exact propensities, bounded
outcomes and no interference, and within-group effect estimates must be
approximately normal. This is a random-effects population model, not an anytime
confidence sequence. DL/HK can under-cover with few groups and extremely unequal
precisions; reports warn below ten groups. A seeded 24-owner heterogeneous test
checks coverage against a 90% floor for a nominal 95% interval. An eight-owner
stress run with precision ratios of 5000:1 covered about 90% in 1000 replicates;
that regime does not support a nominal-coverage guarantee.

E-values remain separate: the dependence-robust arithmetic mean tests the joint
directional null across groups. The product requires an explicit independence
assumption. Neither establishes the sign of a heterogeneous population average.

Optional Gaussian releases use frozen public nuisance predictions and an
explicit propensity floor. The vector of arm sums and squared sums has L2
replacement sensitivity `hypot(2B, B²)`, where `B = 1 / propensity_floor`.
For epsilon at most one, sigma is
`sensitivity * sqrt(2 log(1.25 / delta)) / epsilon`.
Counts, public identity, unit hashes and membership are public under this
adjacency. DP releases suppress the unnoised e-value to the constant one.
The stdlib finite-precision Gaussian sampler is a reference implementation,
not an audited exact-arithmetic privacy engine. Noise on squared sums makes
variance estimation approximate. Privacy budgets compose epsilon and delta
conservatively across releases, with charges persisted before output. A crash
may consume budget without producing a release. Budget identities are immutable.

## Worked workflow

Provision an Ed25519 identity with the security extra, then register its public
key and independently verified owner in the receiver store:

```sh
unimem experiment pool identity '{"tenant":"tenant-a"}' --dest ./tenant-a
unimem experiment pool register ./contributor.json --dest ./receiver
unimem experiment pool privacy '{"tenant":"tenant-a","epsilon":1,"delta":0.00001}' --dest ./tenant-a
unimem experiment pool contribute ./release-request.json --dest ./tenant-a
unimem experiment pool accept ./signed-bundle.json --dest ./receiver
unimem experiment pool report '{"unit_hash":"<64-character-public-content-hash>"}' --dest ./receiver
```

`contributor.json` contains `tenant`, `principal`, `public_key`, `group`, positive
`stake_cents` and future `expires_at`. `release-request.json` contains `tenant`,
`unit_hash`, unique `release_id`, and `rows` with `occasion_id`, binary `injected`,
`outcome` and logged holdout `rate`. Optional `epsilon` and `delta` consume the
configured budget. A release retry is idempotent only for identical inputs.
Inspect `unimem experiment pool --help` for all subcommands.

The local MCP tool `pooled_effect` reads verified contributions in the configured
store. It never certifies a skill. The Hub accepts `POST
/v1/evidence/pool/contributions` with `{"signed": <envelope>}`, an admin API key,
a ledger signer and an operator-managed `HUB_POOL_CONTRIBUTORS_FILE`. That file
is a bounded JSON list of the registration fields above plus `organization`.
The authenticated organization must equal the contribution's tenant. The Hub
stores a tenant-scoped signed envelope and transparency receipt; this route does
not expose another organization's contributions. Identical replay returns 200,
first acceptance 201 and conflicting release bytes 409.

Methods: DerSimonian–Laird, DOI 10.1016/0197-2456(86)90046-2; modified
Hartung–Knapp, DOI 10.1186/s12874-015-0091-1; Gaussian calibration, Dwork and Roth,
DOI 10.1561/0400000042, Appendix A.
