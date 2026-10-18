# 0012: Point-read validation and fenced local work

The G7 protocol uses opaque body-bound versions rather than changing existing
fact IDs, hashes or receipts. It checks declared point reads inside the canonical
writer transaction. A shared local lock orders policy changes, leases and writer
transactions, including writers that began before a policy was activated.

Snapshot mode checks changed existing records, serializable mode checks the
entire declared point-read set, and sequenced mode also checks a durable exclusive
lease before generation and again before commit. These guarantees require a
complete declared point-read set; predicate isolation and distributed consensus
are outside this implementation. The unchanged default supports legacy stores.

Every new fetch-by-ID and conflict response uses canonical scope, lifecycle,
capability, lineage and evidence admission. Stale responses never reveal a
private or erased fact. Explicit semantic merges retain sources and add review
items through `fact_conflicts`; contradiction detection precedes duplicate
suppression for team writes. No scope widening or automatic supersession follows
from a merge request.

Owner-authorized privacy purges bypass generation checks only after canonical
forgetting is committed, and the bypass refuses additions, edits and unrelated
removals. A work lease must not block privacy deletion.

Tests reproduce lost updates under none and prevent them under serializable,
exercise simultaneous readers, expired/stale fences, near-duplicate contradictions
in all three write paths, scoped HTTP fetches and structured conflicts, local and
HTTP SDKs, CLI operations and erasure. An isolated mutation removing the read-set
check is detected by both legacy and SQLite lost-update tests.
