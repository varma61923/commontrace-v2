# UniMem memory telemetry

UniMem pins the OpenTelemetry GenAI conventions to semantic-conventions commit
`f3c6d0c9bb66ebd474668a3a0be25c61226f02c2`. The memory convention was in
development at that snapshot. The authored [contract fixture](../tests/fixtures/otel_memory_contract.json)
records the operation names and attribute types verified against that source.

Enable the optional instrumentation explicitly:

```python
from commontrace import otel_memory
otel_memory.configure(root, enabled=True, include_content=False)
```

Install `commontrace[otel]` and configure the existing OpenTelemetry pipeline
(`COMMONTRACE_OTEL=1`, an SDK exporter and its endpoint) to export spans.
The extra supplies the SDK; an OTLP exporter is a separate deployment dependency.
With no SDK or tracer, canonical operations continue normally. A created span
is not an exporter delivery acknowledgement. Exporter failures cannot turn a
committed memory write or outcome into a reported write failure.

Automatic instrumentation covers `append_facts` as `create_memory`, `add_fact`
as `upsert_memory`, `update_fact` as `update_memory`, canonical fact search as
`search_memory`, and hard fact erasure as `delete_memory`. The upsert distinction
matters: `add_fact` can reinforce an existing fact. Other integrations wrap their
actual operation with `otel_memory.operation`. Deletions admit their targets
before revocation removes visibility. Private targets need an explicitly
authorized context; absent that context, their spans are omitted.

Span names equal the operation name. In-process SDK spans use its default
`INTERNAL` kind. Standard attributes include `gen_ai.operation.name`,
`gen_ai.memory.store.id` (a digest of the canonical local store path),
`gen_ai.memory.record.count`, and a string `gen_ai.memory.record.id` for a single
record. Batches put IDs in `unimem.memory.record.ids`, preserving the standard
field's type. Counts include only admitted records. Invisible records produce
neither spans nor identifying attributes.

Vendor attributes under `unimem.*` carry authenticated writers, the reader,
lineage roots and bounded depth, policy decisions, and optional delegation,
label-ceiling, isolation, read-version, staleness, holdout and diversity fields.
Depth is bounded at 16 with an explicit truncation flag. These diagnostics do
not establish source independence, agent execution or semantic truth.

Query text and record contents are off by default. Explicit `include_content`
uses `gen_ai.memory.query.text` and `gen_ai.memory.records` only when every
requested record is admitted. Records are bounded, redacted JSON encoded into
a string for SDK attribute compatibility. SDK exception recording is disabled;
error spans record the exception class as `error.type`, without exception text.
Operators must also govern their telemetry storage, access and retention.

Canonical outcomes produce separate `unimem.task.outcome` vendor spans for
admitted memory-unit occasions. They carry the actual `unimem.holdout.arm`,
`unimem.holdout.propensity` and `unimem.outcome.succeeded`. They do not pretend
that recording an outcome is a memory search operation. Example Tempo TraceQL:

```text
{ name = "unimem.task.outcome" && span.unimem.holdout.arm = "treated" }
```

Example Datadog filter:

```text
operation_name:unimem.task.outcome @unimem.holdout.arm:treated @unimem.outcome.succeeded:true
```

Filter to one memory ID when comparing success counts, since one task can
contain several memory-unit occasions. Compare treated and control slices,
including failures. Raw dashboard rates are descriptive; use the canonical
AIPW/e-process report for causal estimates under changing propensities.
These illustrative dashboard queries require deployment-specific field mapping
and are unverified against hosted Grafana or Datadog fixtures.

G7 team writes automatically include the validated isolation level and read-set
version digest. `unimem.read.staleness_ms` measures elapsed time since local
validation, not remote cache age, and is omitted when validation did not occur.
These attributes follow the same admitted-record filtering as all other fields.
