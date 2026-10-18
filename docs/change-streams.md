# UniMem change streams and byte erasure verification

Use standing queries when an agent, cache or trainer needs updates to a governed
memory view. Capture is off until an operator creates a subscription. Canonical
fact transactions and lesson writes then stage a durable metadata outbox before
commit. Recovery publishes committed changes idempotently, discards aborted
changes and blocks ambiguous recovery for operator reconciliation. The canonical
conflict queue supplies typed contradiction events. Queues and scans have explicit
size bounds; exceeding them requires maintenance rather than silent omission.

```sh
commontrace stream subscribe '{"identity":"trainer","context":["team:assembly"],"namespace":"facts","ack_seconds":3600}' --dest ./store
commontrace stream tail '{"identity":"trainer","context":["team:assembly"]}' --dest ./store
commontrace stream acks '{}' --dest ./store
```

Subscriptions pin the authenticated principal, scopes, immutable filters and ACK
deadline. `entity` matches a bounded token phrase in the current admitted text;
`predicate` matches the fact category; `namespace` names the canonical namespace.
Every payload repeats canonical source admission. An invisible belief does not
appear in a diff. A previously delivered belief that leaves the authorized view
produces only a minimal tombstone. Provenance commits to the original event;
content is the **current admitted view**, not a historical body snapshot.

The owner-authenticated gateway exposes `GET /v1/stream?identity=trainer` with
repeated `context` parameters and optional `cursor`/`limit`. It returns a bounded
SSE `beliefs` batch. Reconnect with `Last-Event-ID`, committing its cursor only
after parsing the complete event. Retries can repeat a batch; the signed cursor
provides exactly-once progress when the consumer commits it atomically with its
own update. Cursors expire after seven days and bind the subscription and filters. Their encoded sequence
positions reveal root-level event activity; use separate roots for tenants that
must not share this metadata boundary. They are authenticated, not encrypted.
Deferred SSE chunks recheck admission and fail if the view changed.

The MCP `subscribe_beliefs` tool returns the current diff and
`unimem://beliefs/<identity>`. Standard `resources/subscribe` and
`notifications/resources/updated` use bounded 60-second leases; renew through
the tool with the returned cursor. Reading a resource repeats its configured
input cursor until renewal, so an interrupted reader can retry. An in-process
SDK without a request connection still returns diffs and reports notifications
unavailable. There are at most 32 active notification leases per server.

Kafka/Redpanda uses `commontrace[kafka]`. Construct a producer with
`change_stream.kafka_producer(config)` and call
`change_stream.forward_kafka(root, identity, producer, topic, context=labels)`.
Broker authentication, encryption, residency and topic authorization are operator
responsibilities. The adapter rechecks admission before each message and returns
a cursor after successful flush. Deduplicate retries by message key. A broker
transport ACK is separate from an erasure ACK. The low-level `kafka_send` helper
only encodes an already authorized batch; prefer governed forwarding.

## Erasure acknowledgements

Delivery is registered before egress, conservatively including interrupted
transports. Every erase event affecting a delivered target creates a deletion
obligation, including when the consumer is offline. `stream acks` lists pending
obligations. Submit the subscriber's receipt commitment after deleting its copy:

```sh
commontrace stream acks '{"identity":"trainer","context":["team:assembly"],"sequence_number":42,"receipt_digest":"<64 hexadecimal characters>"}' --dest ./store
```

Overdue consumers are cut off from new data until they acknowledge deletion.
They can still submit ACKs after cutoff. These are authenticated downstream
claims; UniMem has not inspected remote disks. Unregistered copies, previously
trained models and bytes already in flight require their own deletion or
retraining controls. The ledger does not claim model unlearning.

## Conditional byte receipts

First perform canonical erasure. Then supply original content fingerprints:

```sh
commontrace erasure verify '{"identities":["<erased-id>"],"strings":["<original sensitive content>"],"vacuum":true}' --dest ./store
```

`certified_erasure.verify_bytes` exposes the same operation without changing
existing certificate hashes. The verifier automatically inventories local memory,
trace and managed skill stores. It scans raw SQLite pages and freelists, JSONL,
Markdown and raw vector files for UTF-8/UTF-16/JSON-escaped exact strings, n-grams
and optional hexadecimal embedding byte patterns. Patterns remain in RAM;
reports store commitments. SQLite cleanup enables `secure_delete`, runs `VACUUM`
and checkpoints WAL; blocked readers remain unverified. The threat motivating
raw vector scans includes recoverable soft-deleted HNSW entries (arXiv:2606.18497).

A signed, transparency-logged **conditional absence** receipt requires prior
canonical revocation, no detected residue, no unverified inventoried store and
all tracked downstream ACKs. The operator must supply correct original content
patterns. File-subset scans are diagnostics and cannot issue a receipt. Limits
are 512 files, 2,048 directories, 64 MiB per file and 256 MiB in total. Links,
unknown or compressed codecs without decoded adapters, encrypted envelopes
without key-destruction evidence, changed files/inventory and uninspected remote
object versions remain unverified. Scanning is an observation of enumerated file
bytes, not an atomic snapshot of every concurrent writer. Backups, process RAM,
storage-device remanence, transformed content and remote replicas are outside
this byte claim. An unverified result never says “purged and verified.”
