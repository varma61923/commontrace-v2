# Committed effect statements

A bank can disclose a randomized memory trial's effect and confidence bound
without sending its episode records to an auditor. The optional RISC Zero guest
computes the same bounded integer estimator as the ordinary audited backend.
The proof establishes committed arithmetic, not that an agent executed an
assignment or that a reported outcome is true.

## Protocol

An owner registers a complete cohort of distinct ASCII episode identifiers and
propensities before **any** cohort outcome exists. Registration requires admitted
canonical source memories; protected hazards cannot enter effect experiments.
The frozen plan commits the cohort Merkle root, scale, confidence level, minimum
propensity, estimator, harm threshold, hiding seed commitment and optional noise
seed commitment. The registration is anchored in the transparency log.

An independent verifier pins this commitment and supplies a fresh 32-byte nonce.
Its challenge is anchored before outcomes exist. SHA-256 rejection sampling
derives assignments at the exact integer propensity; identifiers and the plan
hash bind every draw. The verifier must pin the challenge hash independently,
rather than trusting a hash supplied alongside a proof. Managed collection joins
every frozen episode to the ordinary canonical binary task outcome. Harm mode
uses binary task failure as its explicitly declared proxy; it does not measure
physical injury. Missing members block proving. Sources, revisions, scopes,
consent and export policy are rechecked at use time.

```sh
unimem prove effect '{"action":"register","study":"pilot","entries":[{"episode":"task-1","propensity":500000},{"episode":"task-2","propensity":500000}],"source_ids":["SOURCE_ID"]}' --dest ./store
unimem prove effect '{"action":"challenge","study":"pilot","nonce":"VERIFIER_64_HEX_DIGITS"}' --dest ./store
unimem prove effect '{"action":"assign","study":"pilot","episode":"task-1"}' --dest ./store
unimem prove effect '{"action":"observe","study":"pilot","episode":"task-1","succeeded":true}' --dest ./store
```

Resolve every member before `action: "prove"`. `action: "witness"` explicitly
discloses the owner's complete private witness. For transparent verification,
pass `proof` and `expected_challenge` to `prove verify`, with
`--witness-file private-witness.json`. Verification exits 1 for a mismatched
statement or challenge. These owner operations are unavailable in the browser
console. Public proof outputs exclude secret seeds and episode records.

## Arithmetic and commitments

The scale is 1,000,000; outcomes are integers in [0, scale], and propensities in
[50,000, 950,000]. Managed cohorts contain 2–30,000 rows. The primary effect is
the mean inverse-propensity weighted difference. Division rounds down; its
absolute rounding error is at most 2 parts per million against exact rational
IPW. A secondary self-normalized Hajek difference has error at most 6 parts per
million when both arms exist. The confidence interval is for the primary IPW
estimate, not the Hajek estimate. Harm-rate rounding error is at most 1 part per
million. Signed intermediate arithmetic uses checked i128 in Rust; estimator
sums fit below 2^40, Hajek products below 2^60 and confidence products below
2^73 within this profile. Overflow rejects the witness.

Conservative Hoeffding bounds use upward-rounded logarithms and an integer
ceiling square root, with alpha chosen from 0.05, 0.01, 0.001 and 0.000001.
Coverage assumes valid random assignment, bounded outcomes and the required
sampling/delivery assumptions. It does not address unreported executions or
confounding introduced by deviating from the registered experiment.

Canonical sorted JSON binds the plan and challenge. Cohort leaves are
`SHA256(02 || u16le(id_length) || id || u32le(propensity))`. Outcome leaves are
`SHA256(00 || hiding_seed || u32le(index) || u16le(id_length) || id || bool_byte
|| u32le(propensity) || u32le(outcome))`. IDs sort bytewise. The binary Merkle
tree splits at the largest power of two strictly below its size; internal nodes
are `SHA256(01 || left || right)`. A fresh secret 256-bit seed hides the small
outcome domain; its domain-separated commitment is registered before outcomes.
The final row root and statement digest are anchored in the transparency log.

## Backends

`transparent` is the stdlib default. The verifier receives all rows and
recomputes all commitments and statistics. Its output explicitly says
"complete witness disclosure and audited recomputation; no input privacy".

`risc0` uses the actual RISC Zero **3.0.6** SDK, a Rust guest in `zk/methods/guest`
and a separately pinned host binary/image ID. No development receipts are
accepted. Only succinct or Groth16 receipts are accepted by this privacy
profile. The native verifier checks the cryptographic receipt, typed journal,
externally pinned image ID and challenge, and exact expected statement. The
Python collector also independently recomputes the guest statement before
publication. No remote proving service is enabled. Private witness bytes enter
the local host over stdin; native errors do not echo them.

```sh
python -m pip install -e '.[zk]'
cargo test --manifest-path zk/Cargo.toml --locked -p unimem-effect-core -p unimem-effect-host
cargo build --manifest-path zk/Cargo.toml --locked -p unimem-effect-host --features prove
unimem prove effect '{"action":"configure-prover","program":"/absolute/path/unimem-effect-host","image_id":"AUDITED_64_HEX_IMAGE_ID"}' --dest ./store
unimem prove effect '{"study":"pilot","backend":"risc0"}' --dest ./store
```

Building with `--features prove` requires the separately installed RISC Zero
guest toolchain. Ordinary Rust builds produce a real verifier-only host;
`describe` reports this explicitly. The Python `zk` extra provides public-log
signature dependencies, not a Rust compiler. Audit and independently pin the
guest image ID and host binary; enrollment pins both and rejects binary changes.
Unavailable toolchains produce an explicit error, without replacement receipts.
CI always checks shared arithmetic and SDK verification, and builds the guest
only when its custom toolchain exists. Native Python integration tests use
`UNIMEM_EFFECT_CORE` and `UNIMEM_EFFECT_HOST`; an absent proving toolchain has a
specific skip reason.

`zk_effect.prove_chunked` accepts an explicitly provisioned recursive-circuit
provider for larger cohorts. It checks complete contiguous chunk coverage,
pinned statement identity and the provider's verifier. There is no built-in
large-trial circuit or managed large-cohort registration. Operators must audit
and pin that extension independently; selecting `chunked` without it fails.

## Optional noisy statements

Register `noise_fraction: [3,5]`, `[9,10]` or `[99,100]` before the nonce.
The guest uses a finite exponential mechanism on a 1,000-ppm grid with exact
integer weights. It releases only the noised primary estimate and its enlarged
interval; the exact Hajek statistic is suppressed. The public sensitivity,
epsilon upper bound and delta upper bound are included in the statement.
Profiles requiring epsilon above 20 are rejected.

A fixed-length SHA-256 PRG draw induces at most 2^-256 statistical bias; together
with epsilon ≤20 the approximate delta bound is conservatively 2^-224. The
interval splits its error budget between Hoeffding sampling error and the
finite noise tail, including a margin for that draw bias. DP is conditional on
independent, uniform, secret seeds and the hash/PRG assumptions; neither a
receipt nor a seed commitment proves entropy. Transparent verification reveals
the rows and seeds and therefore provides no input privacy even when its
arithmetic includes noise. One managed public release is allowed per study;
retries return the original bytes and cannot change backend. This restriction
does not provide a global privacy budget across separately registered studies.

Erasing a source purges managed cohorts, keys, proof shards and local cache
entries through ordinary lineage. Previously disclosed proofs or witnesses
cannot be recalled from recipients.
