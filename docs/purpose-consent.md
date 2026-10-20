# Purpose-bound memory and consent receipts

A care assistant may retrieve a patient's exercise limit for improving
healthcare. A shopping assistant using a service-provision capability cannot
retrieve that memory, even if it knows the memory ID. Consent adds restrictions
to existing ownership, admission, capability and finite-policy checks.

Enroll existing admitted memories with an undelegated operator terminal:

```sh
commontrace person purposes --dest ./store
commontrace person consent '{"subject":"customer-42","identities":["fact-id"],"purposes":["https://w3id.org/dpv#ImproveHealthcare"],"controller":"Care provider","expires_at":"2027-01-01T00:00:00Z","collection_method":"Reviewed affirmative consent","context":["project:care"]}' --dest ./store
commontrace person explain '{"subject":"customer-42","context":["project:care"]}' --dest ./store
commontrace person reap --dest ./store
```

Use actual source IDs and an appropriate future expiry. `record:<id>` and
`capture:<id>` select native records and source captures. `inferred` optionally
lists selected IDs whose content is an inference; these appear explicitly in
`explain`. The MCP equivalent is `explain_memory_about_me`. Both explanation
surfaces require the undelegated owner and preserve source scope checks. They
cover explicitly enrolled subject bindings; they do not claim to discover every
implicit reference to a person in arbitrary text.

Receipts record a root-specific HMAC subject pseudonym, controller, purposes,
processing operations, collection method, issuing operator, issue/expiry time,
source digests and inference declarations. Raw subject identifiers are not
persisted. The store's pinned signing identity authenticates the receipt and
bounded state; HMAC is shared-secret authentication, not public non-repudiation.
An append-only transparency event commits the receipt digest. An operator's
assertion records consent; it does not prove that the person consented.

The field profile follows the consent-record concepts of
[ISO/IEC TS 27560:2023](https://www.iso.org/standard/80392.html) and uses an explicit
subset of [DPV 2.1](https://w3id.org/dpv/2.1). `subject`, `controller`, `purposes`
and `processing` correspond to `dpv:hasDataSubject`, `dpv:hasDataController`,
`dpv:hasPurpose` and `dpv:hasProcessing`. Issue and expiry times are the local
profile's validity interval. This JSON profile is not an ISO conformance
certificate or a complete implementation of the DPV ontology. `person purposes`
lists the exact supported terms. Superclasses do not implicitly grant purposes.

`processing` defaults to Collect, Store, Retrieve, Use, Infer, Disclose and Erase;
operators can provide a narrower explicit DPV list. Retrieval requires Retrieve,
export requires Disclose, and derived writes require Store and Infer. No receipt
means existing store behavior. Once enrolled, a memory cannot be silently rebound
to a different subject or broader purpose: create a reviewed new memory.
`person withdraw '{"receipt_id":"..."}'` immediately denies processing.

The requesting purpose comes from the verified G6 leaf capability. Gateway
`POST /v1/recall` accepts optional `purpose`, which must match that verified
purpose. An agent without a capability cannot select its own purpose. Trusted
owner applications can use `purpose.request(root, purpose_uri)`; that context
resets when the operation ends. Caller-supplied memory metadata cannot select a
purpose. Purpose checks cover facts, lessons, traces, native records and capture
buffers, managed artifacts, source-linked derivations and passport exports.
Ownership and subject-consent restrictions are independent of each other.

Expired or withdrawn consent blocks retrieval immediately, even if cleanup has
not run. Schedule `person reap` as an operator maintenance task. Retention purges
enrolled canonical sources and managed descendants, then publishes normal G20
erasure events. Disconnected subscribers must still acknowledge deletion.
Legal holds, protected hazards or unavailable stores leave cleanup pending.

For conditional crypto-erasure evidence, enroll `vault_directory` pointing to
a subject-key vault outside the content store and its backups. Enrollment stores source-linked encrypted snapshot envelopes. Each consent
receipt has an independent destruction key, so expiry cannot destroy keys for a
different live consent. The existing certified-erasure workflow signs evidence
of actual key destruction after local cleanup. Its backup claim covers only
pre-encrypted subject envelopes; enrollment does not encrypt old plaintext
backups. Without a vault, local purge still runs, but cleanup stays pending and
no crypto-erasure certificate is fabricated. Remote deletion and byte-residue
verification retain their separate evidence requirements.

Passports include signed consent scope. Memory credentials include selectively
disclosable purposes and consent, expire no later than consent, and become
inactive immediately after withdrawal. Vault paths are omitted from exported
consent receipts. These controls cannot recall bytes already delivered to an
external recipient.
