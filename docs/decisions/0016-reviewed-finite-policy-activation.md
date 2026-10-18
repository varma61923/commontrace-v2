# 0016: Exhaustive finite policy differences with reviewed activation

## Decision

Use a closed, bounded authorization matrix over authenticated principals and
operator-owned attributes, actions, memory classes, complete label sets and
purposes. Compare the union of both declared domains exhaustively; deny overrides
allow. Include every change in the digest and cap only displayed witnesses.
Reject out-of-bound domains before evaluation.

Activation rechecks the exact policy and current generation under a writer lock
(local) or tenant policy-row lock (PostgreSQL). Any widening requires a signed,
expiring, transparency-anchored review bound to the precise certificate and
old/new hashes. A fresh store's first managed allow requires review. Primary
administration remains available through a narrow policy-proposal artifact
bypass. Ordinary memory operations receive no operator bypass.

Keep owner privacy erasure independent from finite read/write permissions.
Apply policy at actual admission and persistence boundaries, with application-
bound recall/export actions. Hosted state uses its own forced-RLS table rather
than filesystem state shared implicitly between replicas.

The optional pinned Cedar SDK validates and executes real bounded requests;
imports project only the explicit finite domain. Optional OpenFGA/SpiceDB
adapters provide fresh bounded allowlists before supported retrieval limits.
Local provider enrollment is immutable, HTTPS-only and offline by default.

## Limits and alternatives

This is sound and complete for the supported finite language and domain, under
trusted canonical metadata and authenticated application contexts. A sampled
matrix or truncated comparison would not justify a no-widening certificate.
An unrestricted symbolic policy language would require a different verifier.
Remote relationship state/schema, arbitrary external retrieval callbacks,
compromised operators and uninstrumented storage are outside that proof.
Cedar's formal model does not transfer formal verification to this adapter.
See [memory policy](../memory-policy.md) for contracts and operational examples.
