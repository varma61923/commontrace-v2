# Team-memory isolation

UniMem can refuse an agent's fact write when the facts used to generate it have
changed. This is opt-in per local space; an unconfigured store keeps its existing
behavior and canonical record formats. It covers canonical facts in SQLite and
legacy JSONL, including the gateway's `add` and `batch` routes.

| Level | Commit behavior |
|---|---|
| `none` | Declared versions are informational; lost updates remain possible. |
| `snapshot` | A coherent fetch supplies a snapshot. Updating or removing an existing fact requires its unchanged declared version; unrelated point reads may change. |
| `serializable` | Every declared point read must still match under the canonical writer transaction. |
| `sequenced` | The serializable check also requires the current principal's unexpired exclusive work-item lease and fencing token. |

The serializable guarantee assumes a complete declared point-read set, all writes
using the shared store and lock domain, and no undeclared predicate dependencies.
It does not provide predicate locking or multi-host consensus. Snapshot mode can
allow write skew. Raw operator file edits are outside the protocol. Use one
shared local lock domain; separate replicas need an external coordination layer.

## Work through a conflict

```sh
commontrace team isolation '{"level":"serializable","scopes":["tenant:warehouse"]}' --dest ./store
commontrace team read '{"identities":["fact-example"],"context":["tenant:warehouse"]}' --dest ./store
```

Use a real fact ID in the read command. `team read` returns admitted facts and a
`read_set` of `{id, transaction_time}` pairs. The opaque `tx1:` version binds the
exact canonical body, including transaction timestamps, without changing any
existing fact's hash. It distinguishes changes even when timestamps coincide.
Fetching a guessed private ID is refused before returning a body or version.

```python
from commontrace.client import MemoryClient

memory = MemoryClient("./store", context={"tenant": "warehouse"})
snapshot = memory.team_read(["fact-example"])
result = memory.batch(
    [{"statement": "Use the inspected gripper for this load."}],
    read_set=snapshot["read_set"],
)
```

The same methods work with `MemoryClient(url=..., token=...)`. HTTP clients use
`POST /v1/memory/team-read`, then send `read_set` to `POST /v1/memory/add` or
`POST /v1/memory/batch`. Optional fields are `team_space`, `work_item`,
`fencing_token`, `lease_id` and `merge`. Primary authentication and capability
checks apply to these routes. Request bodies cannot select another principal's
scopes. A root with an active isolation level requires a declared team request; an unknown space
cannot silently fall back to `none`.

If another agent changes a read fact, the write returns HTTP 409 with code
`STALE_GENERATION`, currently authorized superseding facts, and the choices
`regenerate` and `merge`. No generated fact is committed. Unknown, erased and
out-of-scope facts supply no private conflict body. Regenerate using a new fetch.
For a semantic merge, fetch fresh versions and submit the proposed statement
with `merge=True`: UniMem records a `fact_conflicts` review item and retains the
source facts. It never resolves that review with last-write-wins. Contradictions
are checked before near/semantic dedup, even if legacy contradiction detection
was disabled. Protected hazards are not superseded.

## Exclusive work and privacy erasure

```sh
commontrace team lease '{"work_item":"inspect-gripper","seconds":60,"context":["tenant:warehouse"]}' --dest ./store
```

In a `sequenced` space, pass the returned lease to `MemoryClient.batch(lease=...)`
or its three identifying fields to the gateway. Reacquisition increments a
durable fencing counter. Old tokens and leases that expire during commit are
refused. Release requires the owning principal, lease ID and exact fence.

Owner-authorized byte erasure has a separate narrow path after canonical
forgetting is committed. It can remove only those revoked facts and cannot add
or edit facts. Privacy deletion does not wait for generation or a work lease.
Existing safety reviews and incident legal holds still apply.

Limits: 64 spaces, 256 point reads per request, 10,000 canonical facts in an
opted-in transaction, 10,000 durable work items, lease TTL 1–3,600 seconds. Locks
serialize policy and lease changes before canonical writes. There is no network
call on this path. Fetches and conflict details always reapply scope, capability,
lifecycle, lineage and evidence admission.

G10 spans add the validated isolation level and read-version digest. The
`unimem.read.staleness_ms` attribute is elapsed time since local commit validation,
not an estimate of remote cache age; it is omitted when no validation occurred.
