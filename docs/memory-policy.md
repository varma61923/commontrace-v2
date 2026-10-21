# Reviewed finite memory permissions

A support operator can allow an agent to recall support facts while denying
export of those facts and access to medical labels. Adding a new purpose or
removing a denial changes the authorized request set. UniMem computes that
change before activation and requires an operator review for any widening.

Policies are explicitly enabled. An unconfigured store retains its existing
behavior. The first managed policy is compared with a deny-all managed baseline,
so its initial allow rules also require review. Activating a policy does not
replace scope checks, origin verification, review, lifecycle, delegation,
information-flow or causal admission. A request must pass every applicable check.

## Closed policy domain

A policy has exactly `version`, `domain` and `rules`. Version 1 declares five
finite axes: principals, actions, memory classes, label sets and purposes.
Principals carry operator-owned IDs, classes and string attributes. Labels are
**complete explicitly enumerated sets**, rather than a vocabulary from which
arbitrary combinations are implicitly authorized. Unknown values are denied.

```json
{
  "version": 1,
  "domain": {
    "principals": [
      {"id": "operator:local", "class": "operator", "attributes": {}},
      {"id": "agent:support", "class": "support", "attributes": {"region": "eu"}}
    ],
    "actions": ["read", "write", "recall", "export"],
    "memory_classes": ["general", "lesson", "trace"],
    "label_sets": [[], ["project:support"], ["health", "project:support"]],
    "purposes": ["default", "support"]
  },
  "rules": [
    {"id": "operator", "effect": "allow", "principal_classes": ["operator"]},
    {"id": "support-recall", "effect": "allow", "principals": ["agent:support"],
     "actions": ["read", "recall"], "purposes": ["support"],
     "memory_classes": ["general"], "required_labels": ["project:support"],
     "attributes": {"region": ["eu"]}},
    {"id": "health-denial", "effect": "deny", "required_labels": ["health"]}
  ]
}
```

Deny overrides allow; the default is deny. Omitted rule selectors mean all
currently declared values. Unknown fields and undeclared selector values are
errors. There are at most 32 principals, 8 attributes per principal, 4 actions,
32 classes, 64 label sets, 32 labels per set, 32 purposes and 128 rules. Both each
policy and the union comparison must fit **65,536 complete request tuples**.
A proposal is at most 128 KiB. A comparison outside these bounds fails before
exhaustive evaluation; it does not sample or silently truncate the proof.

For each tuple in the old/new union, evaluation uses the attributes in that
policy's own principal registry. Domain expansion and changed attributes are
therefore checked too. The diff includes full added/removed counts and a digest
of every change. Only the displayed witnesses are capped at 64 per direction.
Equivalent rule rewrites can have different policy hashes and zero authorized
request differences.

**The no-widening check is sound and complete only for this closed finite
language, declared domain and trusted enforcement metadata.** It does not prove
claims about arbitrary ABAC expressions, all possible future principals, remote
relationships, a compromised operator, or an uninstrumented memory system.

## Identity and actual enforcement

Local principal IDs are `<bound authority>:<bound principal>`; the default local
owner is `operator:local`. Delegated requests use the verified capability purpose;
other local operations use `default`. Memory content cannot nominate identity,
attributes or purpose. A deployment must configure delegation when agents need
separate authenticated purposes. Calling trusted Python APIs under the default
operator context is an operator operation.

Canonical facts use their signed `memory_type`, usually `general`; lessons use
`lesson`, traces use `trace`, managed artifacts use their namespace kind, and
runtime records use their actual record kind (`fact`, `profile`, `episode`, etc.).
Runtime scopes include labels derived from each actual ownership dimension.
Hosted runtime scopes include `tenant:<organization UUID>`; declare those exact
label sets when enrolling a hosted policy.

Checks cover canonical fact/trace admission, canonical fact transactions,
lesson admission and writes, managed artifact reads/writes, runtime record
reads/writes and captures. Causal recall binds the `recall` action while invoking
its retriever. Raw lesson/trace and COGX exports, handoffs, skill publication,
training egress and edge publication check export permission. Source-derived
managed artifacts must also pass their source checks. External retrieval
callbacks remain trusted adapters: they must admit canonical sources or apply
the policy API to authenticated metadata before returning content. Text returned
by an arbitrary callback is not automatically a canonical memory.

Runtime search applies admission before ranking and its result limit. Hosted
runtime search supplies authorized IDs before sparse/ANN candidate selection.
Point reads recheck authorization. Owner erasure is a separate operation with
its existing scope and safety checks; a read/write restriction does not revoke
that owner's privacy erasure right. Mandatory safety procedures remain governed
by their existing protected-memory rules; configure policy domains accordingly.

## Review and activation

Certificates bind the complete policy hash, exhaustive old/new diff, active
generation, issue time and expiry. Reviews bind the exact certificate, both
policy hashes and generation, actual reviewer and a digest of the rationale.
Certificates, reviews and activations create transparency leaves. Activation
recomputes the diff and rejects expired, forged, stale or mismatched receipts.
Local activation holds a writer lock; Hub activation locks the tenant's own
policy row. Concurrent activations of one generation have exactly one winner.

Only the local operator or a primary authenticated Hub admin may administer
policies. The narrow local administration bypass applies solely to the immutable
`memory-policies` proposal artifact while executing an operator policy workflow.
It does not grant general memory access. Default expiry is one hour; configurable
expiry is 60 seconds through 24 hours. HMAC receipts require a trusted shared
secret; configure Ed25519 when public verification is needed.

Save the policy above as `policy.json`. CLI request arguments accept JSON objects
or JSON request files:

```sh
unimem prove policy diff certify-request.json --dest ./store
unimem prove policy certify certify-request.json --dest ./store
unimem prove policy review review-request.json --dest ./store
unimem prove policy activate activate-request.json --dest ./store
unimem prove policy current --dest ./store
```

`certify-request.json` contains `{"policy": <complete policy>}`; save the returned
certificate envelope. A review request contains `certificate` and `reason`.
An activation request contains `certificate` and, for a widening, `review_receipt`
(the complete review response). Certificates cannot replay after activation.
The `diff` command is read-only and does not activate or certify a policy.

The equivalent Hub route is `POST /v1/policy/{operation}`, where operation is
`current`, `diff`, `certify`, `review` or `activate`. Send the same request fields
with an authenticated admin key. Tenant identity comes from authentication;
request fields cannot override it. Hub principal IDs are `key:<API key UUID>` or
`user:<linked user ID>`; direct trusted server operations use `service:internal`.
Policy state is stored in PostgreSQL with FORCE RLS, independently of local
filesystem replicas. A configured ledger signer is required. Signing and
transparency insertion commit in the same activation transaction.

## Optional native Cedar adapter

Install `unimem[policy-cedar]` for the pinned `cedarpy==4.12.2` SDK (Cedar
engine 4.12.0). The core remains standard-library-only. `policy_cedar.export`
produces actual Cedar policy text and a Cedar JSON schema, validates it with the
native SDK, and compares every bounded decision using `is_authorized_batch`.
`import_policy` validates and evaluates actual Cedar text against an explicit
finite domain before building a native policy. No SDK analysis is fabricated.
These operations have an additional **8,192-request** resource bound and a
512 KiB Cedar text bound. Unknown SDK versions fail closed.

CLI/Hub certify accepts `format: "cedar"`, a Cedar string in `policy` and an
explicit `domain`. The certificate records the real SDK validation and finite
comparison. Import is a closed-domain projection: an arbitrary Cedar permit
outside the declared domain is not imported. Export snapshots the declared
principal/entity attributes; changes in an external Cedar deployment require a
new comparison. Cedar's formal model does not make UniMem's adapter a formally
verified implementation.

## Optional relationship prefilter

`policy_relations.configure(root, config)` is an explicit local operator enrollment
for OpenFGA or SpiceDB. No provider is contacted by default. Configuration is
signed, transparency-anchored, size-bounded and immutable. Reconfiguration or
disabling an enrolled provider requires a separate reviewed realm; there is no
runtime switch that can silently remove a restriction. OS ownership of the realm
and its signing key remains a trust assumption.

Configuration fields are `version: 1`, `provider` (`openfga` or `spicedb`), HTTPS
`endpoint`, `store_id`, `model_id`, `types`, `relation`, `subjects` and
`credential_env`. `types` explicitly maps selected canonical namespaces (`facts`,
`traces`, `lessons`, `records`) to provider resource types. `subjects` maps actual
bound local principal IDs to `{type, id}` provider subjects. Unmapped principals
receive no IDs. OpenFGA store/model IDs must be pinned; SpiceDB uses empty strings
for those fields. `credential_env` names a secret-provider reference; no credential
value is stored in the enrollment or reports.

OpenFGA uses its ListObjects API with `HIGHER_CONSISTENCY`; SpiceDB uses streamed
LookupResources with `fully_consistent`. Conditional SpiceDB grants are not
accepted. Authorized IDs intersect canonical scope, lifecycle and content
admission before runtime ranking and indexed fact FTS/ANN candidate limits.
Custom ANN callbacks must support `allowed_ids` when this prefilter is active.
Missing support fails rather than retrying an unrestricted query.

Provider responses are limited to 8,192 IDs and 1 MiB, with a two-second HTTP
request timeout, HTTPS verification and redirects disabled. A single retrieval
uses a fresh response and does not cache grants across searches. Missing
credentials, malformed responses and provider failures deny retrieval. Local
relationship enrollment is currently the supported provider configuration
surface; Hub policy administration does not silently reuse a filesystem provider.

**Relationship state and schema changes are external to the finite proof.** The
certificate covers the finite policy authorization matrix. It does not certify a
remote provider's model, availability, absence of truncation, or future changes.
Provider-side truncation can withhold legitimate results; it cannot add an ID
outside the returned allowlist. Canonical memory writes, deletion and downstream
erasure acknowledgements retain their existing independent checks.
