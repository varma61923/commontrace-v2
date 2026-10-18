# 0011: Pinned holders, monotone delegation and committed lineage

Accepted for the G6 implementation.

Capabilities narrow existing primary authentication and immutable operator pins.
A portable MAC prefix is insufficient authority to delegate: local children also
require the actual parent holder's signature. External chains use pinned
Ed25519 keys. Every request carries a fresh nonce and a possession signature over
its exact bytes and purpose. PostgreSQL supplies the Hub's shared replay fence;
SQLite supplies the local one.

Read filtering precedes deduplication, sparse ranking and dense top-k. Writer
identity and integrity cannot exceed the authenticated holder, enclosing writer,
or leaf ceiling. Optional delegation fields are omitted from existing canonical
bodies when no capability is active, preserving prior signatures and protocols.

Runtime and capture provenance uses qualified root-derived identities and a
transactional metadata outbox. Failed or rolled-back writes cannot replace
committed provenance. Recovery publishes committed lineage once and blocks
admission when metadata cannot be recovered. This also extends the existing
change stream to runtime records and captures.

Revocation is reversible quarantine, separate from canonical privacy erasure.
An independent, currently valid authorization must match current content before
release. Bounded lineage and instrumented read graphs propagate impact without
claiming complete observation of arbitrary downstream systems. Cached admission
is invalidated by key/configuration/lineage/revocation changes and the next
alternate-chain expiry.

Capability-enabled Hub MCP uses stateless JSON requests. This keeps tenant and
leaf authorization attached to each request rather than an enduring session.
Handoffs replace incoming histories with recipient-admitted, redacted memory
and persist sticky labels for the claim lifetime.

Validation targets widening, prefix stripping, changed keys, replay races,
purpose/body substitution, tenant and class isolation, actual primary scopes,
rollback and post-commit recovery, exact qualified impact, alternative-chain
expiry, native ANN filtering, CLI/MCP/HTTP contracts and actual OpenTelemetry
span attributes. Standards fixtures check names and encodings; they do not
certify interoperability or regulatory compliance. See [capabilities](../capabilities.md).
