# Sealed buyer trials

Create a signed `cleanroom.listing` with a content commitment, opaque seller-side
handle, MinHash signature and operator-established stake. `cleanroom.trial`
requires trusted offer and sandbox-attestation verifiers. Its runner receives a
holdout action and returns only a binary outcome; the buyer API never returns
lesson content. A production sandbox/enclave is a deployment plugin. Python
interfaces alone cannot hide plaintext from a compromised host.

Escrow settlement requires a ledger-attested valuation certificate, a positive
lower bound and current directional e-process support in the buyer's canonical
experiment. Near-duplicate MinHash clusters share one reward and split it across
established sellers; adding duplicate identities cannot increase a cluster's
reward. MinHash overlap discounts the price. Significant canonical harm permits
stake slashing. Operator-established seller identity and stake enforce the Sybil
boundary; self-created accounts are insufficient.

`private_effect` releases clipped fixed-size cohort means with Laplace noise and
per-authenticated-querier basic epsilon composition. The first budget is
immutable; repeated query ids return the same noisy release. Authenticate query
identity and cohort size outside the API; coordinate accounts to prevent budget
reset or collusion. Existing `federation.randomized_response` remains available
for binary cohort releases. Gaussian DP and a global cross-account accountant
are not provided by this specialization.

`StripeConnect` accepts a configured transport and defaults to disabled. Tests
mock the provider; no live payment is made. Escrow ledger release and an external
money transfer are separate auditable operations.
