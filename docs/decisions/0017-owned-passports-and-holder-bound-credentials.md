# 0017: Exact passport preservation and nonce-bound credential verification

## Decision

Commit canonical memory snapshots with content addresses, a Merkle manifest and
an Ed25519 signature over the entire producer manifest. Preserve all original
signed fields on import and put independent screening metadata in a companion
review artifact. A pinned signature authenticates a source; it never auto-admits
assertions, grants capabilities or proves truth. Summary imports remain visibly
unverified, scanned, redacted and held for review.

Align field names with PAM 1.0 while explicitly identifying UniMem's SHA-256
encoding. Require an explicit model digest and mark its identity as
operator-declared, without claiming model execution attestation. Keep G6 segment
grants intact and require receiving-realm authorization.

Use the real RFC 9901 SD-JWT construction, W3C VC 2.0/VC JOSE fields, Ed25519,
random salted object disclosures, issuer pins and mandatory holder binding.
Fresh verifier nonces bind the exact audience and disclosed serialization and
are consumed once. The fixed bounded fact context and decoys hide field
occupancy. No verifier callback can recover undisclosed canonical values.

Use actual W3C bitstring status encoding. Preserve unused-slot cover and revoke
interrupted reservations instead of reusing their indices. Local verification
uses live state. Foreign verification requires an issuer-signed status snapshot
bound to the verifier's new nonce and audience; a holder-supplied recent cached
list alone does not establish freshness. This is a UniMem freshness extension,
not a claim that every generic VC consumer implements the protocol.

## Limits

This is a bounded object-disclosure profile, not every SD-JWT construction or
PAM wire format. Truth, model execution, uncompromised operator keys and global
remote erasure are not proved. Received memories remain in review. Delivered
plaintext still needs downstream deletion and G20 acknowledgements. Status
quotes describe issuer state at that check, not all later concurrent changes.
See [owned memory passports](../memory-passport.md) for the actual surfaces,
resource limits and privacy assumptions.
