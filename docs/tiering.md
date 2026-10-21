# UniMem managed payload tiering

Use tiering for managed agent, user or robot namespace payloads that reference
canonical governed memories. Canonical source records and signed audit metadata
stay local; tiering does not move a mixed-tenant legacy SQLite database. The
reference implementation manages bounded JSON payloads, including embedding
generations, and works without a cloud SDK.

```sh
unimem tier residency '{"namespace":"assembly","region":"eu","allowed_regions":["eu"],"cold_region":"eu","scopes":["project:assembly"]}' --dest ./store
unimem tier status '{"namespace":"assembly","region":"eu","context":["project:assembly"]}' --dest ./store
unimem tier hibernate '{"namespace":"assembly","region":"eu","context":["project:assembly"],"idle_seconds":3600}' --dest ./store
unimem tier warm '{"namespace":"assembly","region":"eu","context":["project:assembly"]}' --dest ./store
```

Configuration is immutable. Reads, writes, pre-warm, migration and embedding
operations require an allowed region and admitted namespace scope. Payloads
inherit the union of namespace and actual source scopes. Direct managed-store
reads and writes require an established residency context, including reads of
artifact dependencies. Region tags assert operator-managed placement; they are
not geolocation attestation. Operator processes must derive the actual execution
region from trusted deployment configuration.

## Hibernation and recovery

`tiering.put(root, namespace, identity, body, region=..., sources=[...],
context=[...])` writes an immutable managed payload. `get` re-admits its sources.
Hibernation checks idle time and every active source's protected lineage. Safety
memories and derivatives of protected sources cannot be hibernated.

Migration copies and verifies every payload before withdrawing any hot file.
The cold state commits before hot deletion. An interrupted copy leaves the hot
state authoritative; retries are safe. Warming restores only currently active
payload digests, commits the hot state before cold cleanup, and records pending
cleanup so an interrupted deletion can be retried. Erasure removes all managed
versions and cold copies without resurrecting an erased generation. Shared
content remains while an unrelated active artifact still references it.
Namespace journal locks serialize puts, hibernation, warm and erasure. On POSIX,
file and parent-directory synchronization precedes authority changes and
withdrawal. Filesystem durability still depends on the storage stack honoring
those operations. A namespace can reference at most 512 distinct active sources
for migration admission.

The default cold tier is `memory/tier-cold`. Install `unimem[tier]` for S3
or GCS, create `S3Cold(bucket, region)` or `GCSCold(bucket, region)`, and register
the backend in the trusted operator process with `tiering.register_backend`.
S3-compatible endpoints use an injected configured client. Tests use fake
clients and never contact cloud services. Keys incorporate a secret-backed
tenant commitment, so identical namespace names in different roots do not
collide in a shared bucket. Bucket policies, credentials, actual region placement,
retained object versions and backups remain deployment responsibilities.
Deletion receipts name configured cold tiers but do not prove removal of cloud
versions. Byte verification explicitly lists uninspected remote tiers as
unverified.

## Pre-warm before the turn

```python
from unimem import tiering
from unimem.client import MemoryClient
from unimem.robotics.bridge import RobotBridge

warm = tiering.prewarmer(root, "assembly", region="eu", context=["project:assembly"])
client = MemoryClient(root, context={"project": "assembly"}, prewarm=warm)
bridge = RobotBridge(transport, embodiment="arm", site="assembly", skill="pick",
                     robot="arm-7", prewarm=warm)
```

The client invokes `session-start`; the bridge invokes `robot-boot` and
`task-assignment` before recall. Portable `agent_hooks` session-start payloads
can include `tier: {namespace, region}`; admission uses the authenticated agent's
context, not caller-supplied scope labels. Hooks are opt-in. A hook must complete
before the user turn or robot task to remove cold latency from that operation.
This is an integration condition, not a guarantee about arbitrary object-store
latency. A failed pre-warm does not silently serve from a disallowed region.

Run `python -m benchmarks.bench_tiering --iterations 100` for measured local
post-warm p50/p95/p99. The focused test checks a generous 200 ms local hot-read
budget; results depend on disk and canonical source admission costs.

## ACL filtering and embedding generations

`tiering.native_recall` narrows a tenant/namespace-bound vector index using
canonical admitted IDs in its native `allowed_ids` filter. Lance and PostgreSQL
implement native predicates; every returned hit is admitted again. This follows
the predicate-pushdown motivation of ACORN (arXiv:2403.04871). Exact SQLite tests
match filtering the complete candidate set before top-k. Approximate indexes
can have different recall after filtering; no universal ANN equality is claimed.

`unimem tier reembed` accepts an operator-pinned model fingerprint,
validated precomputed vectors, and held-out queries with old/new query vectors
and admitted gold IDs. It builds a separate immutable generation, measures the
recall delta on the same candidate universe and cuts over an atomic signed
pointer only when the configured minimum delta is met. `tiering.recall` reads
that pointer and performs bounded exact cosine retrieval; external native
indexes use their own namespace-bound adapter. The operator supplies truthful
model fingerprints and genuinely held-out queries. Recall is an **estimate**,
not causal evidence or a deployment guarantee. A first generation reports that
there was no previous baseline. Vector limits are 128 candidates, 8,192 dimensions
and the managed 256 KiB payload bound. `unimem tier rollback` restores the
previous admitted generation with one command.

The owner-authenticated `POST /v1/tier` gateway accepts
`{"operation":"status","request":{"namespace":"assembly","region":"eu","context":["project:assembly"]}}`.
The same bounded operations support residency, hibernate, warm, reembed and
rollback; a denied region or scope returns 403 and malformed fields return 400.
Container authentication further narrows the namespace labels.
