# Owned memory passports and selective disclosure

An operator can move reviewed memory to another assistant while preserving its
source metadata and verification evidence. A recipient verifies a pinned issuer
signature, sees the original signed bundle, and reviews its contents before
admission. A valid signature does not make the assertions true or safe to follow.

For a single fact, the operator can issue a holder-bound credential. The holder
can disclose the statement while hiding its subject pseudonym, model digest,
confidence and other fields. The verifier checks issuer and holder signatures,
expiry, its own fresh nonce and current checked revocation state.

## Passport format and trust

A UniMem passport contains five `components`: `episodic`, `semantic`, `procedural`,
`working` and `identity`. Entries preserve the full canonical JSON snapshot,
provenance, source confidence when available, timestamps, labels, `consent_scope`
and optional per-entry `capability_tokens`. Unknown source confidence is `null`.
No consent enrollment is represented as `status: "not-configured"`; an absent
receipt is not a fabricated consent grant.

Each entry has a SHA-256 content address over its complete canonical metadata
and content. Ordered, domain-separated Merkle leaves bind all entries. The
Ed25519 envelope binds the entire manifest, including `source_agent`, root hash
and producing model digest. Changing only the producer metadata also invalidates
the signature. The bundle's address is the digest of the complete manifest.

The field names align with Portable Agent Memory 1.0, arXiv:2605.11032v1. UniMem
uses SHA-256 rather than PAM's BLAKE3 and its own schema; **PAM wire compatibility
is not claimed**. The mapping is:

| PAM concept | UniMem field |
|---|---|
| Five memory components | `components.episodic/semantic/procedural/working/identity` |
| Content-addressed entry | `id: sha256:<digest>` |
| Provenance parents | `parent_ids` and complete `provenance` |
| Entry creation/version | `created_at`, `version`, original content timestamps |
| Producing agent | `source_agent.name`, `model_digest`, `runtime` |
| Merkle commitment | `root_hash: sha256:<digest>` |
| Capability-scoped segments | Per-entry `capability_tokens`, preserved on import |

A producing model must have an explicit `sha256:<64 lowercase hexadecimal>`
digest. This maps to the SHA-256 subject digest used by OpenSSF/Sigstore model
signing manifests. The output states that the digest is operator-declared;
no training, inference execution or external model-signing bundle is attested.

Passports use the `security` extra for portable Ed25519 signatures. The core
parser and Merkle code import no cryptography dependency. `person identity`
creates a private signing seed and returns its public identity. It does not
change the store's existing origin-signing algorithm. Export requires the owner,
canonical admission, scope, policy export permission and generalization checks.
Sources are checked again before signing, and controlled erasure holds the same
incident lock while mutating them. Indexed fact exports use canonical point
reads rather than materializing the full corpus.

The profile accepts at most 64 sources, 32 KiB of canonical content per source,
and a 256 KiB manifest. Signed artifacts must fit the managed 256 KiB blob limit;
choose smaller exports when metadata or signatures exceed that bound. JSON
source fields, including nested metadata, remain intact. Lessons and traces keep
their full Markdown bodies and frontmatter; YAML dates use ISO JSON strings.
When a source has no usable creation timestamp, `created_at` is the passport
entry's creation time and the original source value remains in `content`.

Per-segment grants must be real, currently valid G6 capabilities that authorize
the exported namespace, class and labels. Export rejects forged, expired,
revoked or insufficient grants. These grants constrain authorization; they do
not encrypt plaintext already delivered. A receiving realm preserves the tokens
and independently reauthorizes them. Import never enrolls a key or grant by
trusting a key embedded in the bundle.

## Export, import and review

Install the optional signature support:

```sh
python -m pip install 'unimem[security]'
unimem person identity --dest ./sender
unimem person export export-request.json --dest ./sender
```

An export request contains `identities`, `model_digest`, optional `source_agent`,
`context` and optional `grants` keyed by selected source ID. An example is:

```json
{
  "identities": ["replace-with-an-admitted-memory-id"],
  "model_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "source_agent": "support",
  "context": ["project:support"]
}
```

Use the actual model digest and source IDs. The result includes `bundle`, an
export anchor and the public key. Pin that key through an independent channel
before importing it. The receiver explicitly runs `person trust` with an
`issuer` and `public_key`. `person import` then takes `{"bundle": <bundle>}`.
The exact original bundle is stored separately from review assessments, so a
round trip does not discard fields or alter signed bytes. Receiving context
adds restrictions to source labels.

Imported assertions remain `status: "review"`, `admitted: false`. Source signature
verification is recorded separately from truth or safety. Canonical facts,
lessons and retrieval indexes are not populated automatically. Existing curation
and review workflows decide what to admit under the receiving realm's authority.

For a pasted assistant summary, use `person stage` with `text`, `source` and an
optional `context`. A local file can be staged from the operator terminal:

```sh
unimem person stage '{"source":"assistant-summary"}' --file ./summary.txt --dest ./receiver
```

JSON string lists, `{"memories": ["..."]}` and paragraph-separated text are
supported. Inputs are bounded to 128 KiB and 32 candidates of at most 32 KiB.
Each candidate is marked `unverified, self-reported via <source>`, scanned by
`injection_guard` and `defense`, redacted for known PII, and held for review even
when clean. Raw secrets and PII are not retained in summary staging; only their
content digests and finding types remain. This heuristic scan is not a proof
that arbitrary text is free of injection.

## VC 2.0 and SD-JWT profile

`credentials.py` implements the object-disclosure construction in RFC 9901 with
W3C VC Data Model 2.0 fields and the `vc+sd-jwt` media type from VC JOSE/COSE.
The bounded memory-fact profile uses Ed25519 (`alg: EdDSA`), explicit pinned
issuer/key IDs, SHA-256, 32-byte random salts and 16 shuffled commitments including
decoys. The fixed JSON-LD extension context does not reveal which private fields
were populated. Recursive and array disclosures are outside this profile.

The complete issued token contains every disclosure and is private to the
holder. A presentation contains only selected disclosures and a mandatory
`kb+jwt` holder signature. The issuer-bound `cnf.jwk` determines the holder key.
Its binding covers the exact serialized disclosure set, verifier audience,
nonce and issue time. Verifiers never look up undisclosed claim values. Selected metadata and a verifier's existing knowledge can still support inferences; selective disclosure is not a claim of anonymity or freedom from correlation.
Signatures authenticate the issuer's assertion, not factual truth. Factual
credential issuance also respects the G5 factual gate for personal opinions.

A practical terminal flow is:

1. `person configure-credentials` with an operator-controlled HTTPS `status_url`.
2. `person holder-key` with an `output` filename, returning the holder public key.
3. `person issue` with a canonical fact `identity`, `holder_public`, actual
   `model_digest`, `context`, and optional `subject` and `expires_seconds`.
4. `person challenge` at the verifier with its `audience`.
5. `person present` with the private `issued` token, selected `disclose` fields,
   `holder_key_file`, and that exact `audience` and `nonce`.
6. `person verify` with the presentation, audience and nonce.
7. `person revoke` with `credential_id` and a rationale when needed.

Each operation accepts a JSON request object or file and `--dest`. Private holder
keys are created exclusively with owner-only permissions and never returned.
Signing from holder files and local file staging are terminal operations, not
browser-console file access. Credential issuance, trust enrollment and revocation
require the owner. The default lifetime is one day, bounded from one minute to
one year and capped by the canonical source's expiry/validity end. Private
subject IDs use a keyed pseudonym rather than logging the supplied identifier.
The full issued token is held in a deletable, source-linked managed artifact.

The local MCP tools are `credential_challenge` and `verify_credential`. Their
verification result contains only the disclosed claims. They are available with
lesson approval disabled; neither operation admits memory or grants access.
Verifier challenges last 60 seconds, bind the actual requesting principal, and
are consumed atomically. Concurrent verification of one nonce has one winner.
At most 1,024 unexpired challenges are held; expired state is removed on renewal.

## Revocation, freshness and erasure

Revocation uses actual W3C `BitstringStatusListEntry` fields and signed
`BitstringStatusListCredential` values. Lists contain 131,072 bits, use leftmost
index ordering, GZIP compression and multibase base64url (`u`). Unused slots
provide cover for unrevoked credentials; they do not expose issuance count in
the public bits. Failed issuance reservations are revoked and their indices are
never reused. There are at most 4,096 managed credentials per realm in this
bounded local profile. Revocation is monotone for an explicitly revoked ID.

`person status` returns a signed list for publication at the enrolled URL. No
hosting, background fetch or network availability is invented. Verification of
a local issuer checks live local state and overrides a supplied stale list.
Foreign verification needs an explicit operator-pinned public key and a fresh
issuer quote made for this verifier's nonce and audience. Request that quote at
the issuer with `person status` using the challenge's `nonce` and `audience`, then
pass its `token` as `status_token` to verification. A merely recent cached list
cannot substitute for that quote. The quote expires after 60 seconds and must
postdate challenge issuance. Untrusted credential URLs are never fetched.

The checked state is a snapshot at the issuer's freshness quote; concurrent
later changes require a new check. Key pins, issuer signing keys and trusted
realm storage are assumptions. The profile does not claim instant knowledge of
all remote changes during a network partition.

Known canonical erasure prevents a new passport export or credential issuance,
purges source-linked passport/credential artifacts, and makes local credential
verification fail. Immutable audit leaves contain hashes and status metadata,
not the private issued disclosures. Already delivered plaintext and remote
copies require the G20 acknowledgement channel and downstream deletion; a
signature cannot retract bytes a recipient already downloaded. Consent and
subject-vault configuration are represented explicitly rather than inferring
legal consent or universal backup erasure from a signature.

Native records and captures created before portable-reference enrollment can be
selected explicitly as `record:<id>` or `capture:<id>`. Export returns their
canonical references in `source_aliases`; enrollment preserves the source bytes
and rechecks admission. Native erasure purges linked managed bundles using typed
references. Cross-scope record/capture ID shadowing is refused. A delegated
canonical erasure cascade requires an authenticated native mirror before it can
erase a native row with the same ID.

[Purpose-bound consent](purpose-consent.md) attaches enrolled consent scopes to
exports and credential claims. Disclosure must be permitted, credential expiry
is capped by consent, and withdrawal immediately deactivates local status.
