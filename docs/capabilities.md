# UniMem delegated memory capabilities

A capability narrows an already authenticated agent. It names exact namespaces,
operations, memory classes, purposes, confidentiality labels, an integrity
ceiling, an audience key, authorized delegate keys and an expiry. Unknown
caveats, wildcards and widening fail closed. A ceiling limits the authenticated
writer's role; it cannot turn an agent into a user or operator.

This is UniMem's encoding inspired by
[draft-niyikiza-oauth-attenuating-agent-tokens-01](https://www.ietf.org/archive/id/draft-niyikiza-oauth-attenuating-agent-tokens-01.html),
published 15 June 2026 and expiring 17 December 2026. It is not JWT/AAT wire
compatibility. Local chains use stdlib HMAC-SHA256. Cross-organization issuers use
Ed25519 through `commontrace[security]`, with public issuer and holder keys pinned
by the receiving operator. Keys supplied by a token never establish trust.

## Enroll and delegate

The operator registers each holder against its actual primary credential identity:
Gateway agent ID, Hub API-key prefix, or Hub `user:<uuid>` identity. Enrolled agents
require capability headers by default. Removing headers cannot restore ambient
credential authority. Operator credentials should remain separate from agent
credentials. An explicit `require_capability: false` enrollment is for trusted
operator credentials that retain their existing authority.

Create a 32-byte private holder key with file mode `0600`; keep the client copy
private. The operator can register it using JSON files:

```bash
commontrace team register-capability-agent enrollment.json --dest /srv/unimem
commontrace team grant grant.json --dest /srv/unimem
commontrace team attenuate attenuation.json --dest /srv/unimem
commontrace team revoke revocation.json --dest /srv/unimem
```

`enrollment.json` names `identity`, `principal`, `secret_file`, and `clearance`.
The command returns metadata and the managed private-key path, never secret
bytes. Ed25519 registration uses `algorithm: "ed25519"` and a hex `public_key`;
the external holder retains its private key. Registrations and issuer pins are
immutable. Revoke the old chain and enroll a new key ID to replace a holder.

A grant file has this shape, with an explicit future timezone-aware expiry:

```json
{
  "caveats": {
    "namespaces": ["facts"],
    "operations": ["read", "write", "export"],
    "classes": ["general"],
    "purposes": ["support"],
    "labels": ["project:assembly"],
    "delegates": ["inspection-agent"],
    "ceiling": 1,
    "audience": "coordinator-agent",
    "expires_at": "2026-10-18T12:00:00+00:00"
  }
}
```

Replace the example expiry at issuance: root lifetime is at most seven days.
Both audiences must be enrolled with sufficient clearance. Attenuation takes
`token`, narrower `caveats` and `secret_file`. Offline Python attenuation uses
`capabilities.attenuate(token, caveats, key=parent_holder_key)`. Every link binds
its complete parent prefix. A local child needs the parent's holder-key
signature as well as the previous chain MAC, preventing a descendant from
stripping an intermediate and forging a different branch from exposed prefix
MACs. Chains are bounded to 16 links and 64 KiB of canonical JSON.

For an external issuer, `capability_io.pin_issuer` pins the public key and the
maximum acceptable policy. The external issuer signs a root with
`capabilities.grant(..., algorithm="ed25519")`. The authenticated receiving
organization remains the token's tenant; an issuer signature never overrides
primary tenancy.

## Requests and supported routes

Each HTTP request needs a fresh holder proof in addition to normal credentials:

```python
from commontrace import capabilities, frontier_protocol

headers = capabilities.headers(
    token, key=holder_key, holder_algorithm="hmac-sha256",
    method="POST", target="/v1/memory/search", raw=body_bytes,
    purpose="support", nonce=fresh_random_nonce, now=frontier_protocol.now(),
)
headers["Authorization"] = "Bearer " + primary_gateway_credential
```

Headers are `X-UniMem-Capability`, `X-UniMem-Capability-Proof` and
`X-UniMem-Purpose`. Proofs bind the exact method, raw target including query,
body hash, token, tenant, audience, purpose and nonce. They expire after at most
30 seconds. Generate headers for each request using its actual serialized bytes;
a static header dictionary cannot authenticate a sequence of SDK requests.
Transport/proxy header limits may be smaller than the protocol's token bound.

The Gateway accepts delegated search, add, batch, record-put, record-search,
record-forget, and `GET /v1/stream`. Other authenticated operations with a
capability fail closed. Normal public health remains public. Existing primary
agent/container restrictions still apply. Stream delivery rechecks current
admission in the captured request context; an expired proof stops delivery.

Facts use namespace `facts` and their configured memory type. Lessons use
`lessons` / `general`; local traces use `traces` / `general`. Runtime records use
`records` and the record kind (`fact`, `episode`, `directive`, and others);
captures use `records` / `capture`. Canonical scope labels and runtime ownership
dimensions must all fit the leaf's allowlist. Hidden fact classes are removed
before deduplication or conflict indexing. Restricted runtime search computes
term statistics from admitted records, rather than the private FTS corpus.

## Hub deployment

Set `HUB_CAPABILITY_DIRECTORY` to an operator-owned persistent directory. Each
organization has a separate root at `<directory>/<authenticated-org-uuid>`.
Register holder principals there and issue grants with `tenant_id` equal to that
organization's UUID. Runtime record clearances also include
`tenant:<org-uuid>`. Share the configured metadata volume and issuer pins across
replicas; an organization cannot select its root with a request header.

Apply migration `fd25e7f9a1c3`. Proof nonces are consumed atomically in PostgreSQL
`capability_nonces`, with forced row-level security and tenant isolation. Local
SQLite nonces are not the Hub replay boundary. Authentication and rate limits
run before capability verification. The key's existing read/write/admin scopes
remain mandatory.

Hub REST supports trace search/contribution and record put/search/forget. Hub
MCP supports trace search/get/contribution/amendment/deletion, plus read-authorized
initialization, notification, ping and tool-list protocol messages. Other tools
with delegated headers are denied. Capability-enabled Hub MCP uses stateless
JSON responses so sessions cannot retain another request's delegation context.
An HTTP client must sign each protocol request, including handshake messages.

Scope and class predicates run before sparse ranking, document-frequency
selection and record hydration. Dense record search supplies native allowed IDs
before ANN top-k and rechecks canonical records afterward. Its capability-mode
admission window is bounded to 2,000 current authorized records; larger spaces
need narrower scoped stores. Related trace targets are filtered before their
identifiers are returned. Fleet-wide causal summaries are omitted from restricted
responses instead of being presented as scoped evidence.

Runtime lineage metadata enters a canonical SQLite/PostgreSQL outbox in the
source transaction. Rollback cannot replace committed provenance. Publication
is idempotent; if publication fails after commit, the write remains committed
and admission requires successful recovery. The queues retain signed commitments
and full delegation chains, not source bodies. Hub trace idempotency keys are
isolated by delegation-chain digest; a renewed chain starts a separate retry
namespace.

## Handoff bundles

`commontrace team handoff` accepts `identities`, `recipient`, `context`, and an
optional `seconds` up to 300. MCP exposes `handoff_bundle` with the same fields.
Only sources admitted to both sender and recipient enter the bundle. The sender
needs an export request when delegated. The bundle carries IFC presentation,
credential/PII redaction, source commitments and inherited labels. Protected
hazard text remains intact within authorized clearance. Tenant canaries block
egress.

The signed claim binds the recipient's registered key, source policies, labels
and expiry. The payload lives in deletable managed storage. Recipient redemption
uses `capability_io.receive` inside a fresh read proof, checks every source and
class again, and consumes the claim once before returning content. A changed,
erased, revoked, or inaccessible source blocks redemption. Successful redemption
persists sticky labels for the claim's remaining lifetime, including across
subsequent requests. Writes inherit those labels or are refused.

`capability_io.agents_input_filter` returns an OpenAI Agents SDK handoff callback
that replaces `input_history` and clears `pre_handoff_items` and `new_items`.
It preserves the runtime context. The authored contract fixture pins SDK commit
`9abc9182d06dbd28330af888306031269d57a175`; no SDK is imported in core. The callback
contract is tested without running an agent. `a2a_extension` provides the
negotiated `urn:unimem:handoff:v1` JSON extension; transport interoperability is not
asserted by that shape alone.

## Revocation and limits

Revoking any chain prefix blocks its writes and authenticated descendants.
A memory can be released only by a still-valid independent chain whose signed
authorization matches the current content hash. Another grant by itself cannot
release it. Alternate-chain expiry invalidates a cached release immediately.
Natural expiry does not retroactively invalidate an otherwise legitimate past
write; explicit revocation does.

The G8 read graph adds observed downstream output dependencies. Output identities
stay qualified, so an agent-supplied output name cannot quarantine an unrelated
canonical fact. Diagnostics report only admitted local sources and bounded,
instrumented dependencies; uninstrumented reads and Hub source bodies that are
not present in the local store are outside the local impact report. Hub reads
still enforce the shared revocation and lineage metadata.

Quarantine events retain their type and do not create a false byte-erasure
certificate. Runtime/capture lineage uses root-derived qualified identities so
an agent-controlled record ID cannot overwrite a canonical fact's provenance.
Actual erasure retains its downstream acknowledgement obligations.

Trusted host files, operator enrollment, primary authentication, server clock,
and configured shared storage are assumptions. These checks govern UniMem reads
and writes; they do not sandbox arbitrary agent code. Bounds include 256 agent
pins, 10,000 lineage objects per scoped root, 2,000 affected quarantine nodes at
depth 16, and bounded signed journals. Exhaustion fails closed and requires
scoped namespaces or operator maintenance.
