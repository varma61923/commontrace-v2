# Lossless hosted AtomicFact envelope

Date: 2026-10-12. This closes the explicit conversion contract between the
existing local AtomicFact wire format and the versioned MemoryRecord used by
the hosted record API. It does not claim that every local fact operation or
evidence resolver has hosted parity.

The existing `fact_store._event_row` provided the starting pattern: preserve
the full local payload and mirror validity, recorded time, evidence, confidence
and source traces into the envelope. The graph query below located that path,
AtomicFact, MemoryRecord and the local/hosted journals before source inspection.
No competitor code was needed for this wire adapter. Existing CommonTrace
temporal and evidence rules remain authoritative.

```bash
graphify query 'AtomicFact MemoryRecord _event_row scope fact envelope' \
  --graph /tmp/commontrace-nextgen-final-20261012b/graphify-out/graph.json \
  --budget 1400
```

## Design

The public `atomic_fact_record(fact, version, tenant=...)` and
`atomic_fact_from_record(record)` helpers live in `commontrace.memory_record`.
An explicit `provenance.atomic_fact_format: commontrace.atomic-fact.v1` marker
distinguishes this bridge from an ordinary generic fact record. Payload keeps
every field returned by `AtomicFact.to_dict()`, plus `text=statement` for shared
search. Envelope version does not replace fact revision or evidence revision.
Unrecognized fields, missing fields, malformed receipts, non-finite values and
coercible but incorrect types are rejected. Reconstruction instantiates the
validated dataclass directly: `_coerce_fact` would round confidence, normalize
scope/order, overwrite evidence-bound confirmation counts and manufacture blank
timestamps/revisions, violating this contract.

Recognized colon labels map to tenant/user/agent/session/repo dimensions;
conflicting dimension values are rejected. Unknown labels retain their order
in envelope labels, and the exact original array remains in payload. A hosted
tenant may bind an otherwise tenantless fact; it cannot override a tenant in
the original scope array. Decode checks every mirrored field and scope.

Only unforgotten active facts map to active envelope status. Superseded,
invalidated, deleted and forgotten facts map to deprecated; retired maps to
retired. Each original status stays in payload. Envelope valid-until is the
earlier of original validity end and expiry, comparing actual instants with
naive timestamps treated as UTC. Original valid-until and expiry strings remain
exact in payload. Expiry never sets `forget_after`: hiding an expired belief
does not authorize erasing its audit/history.

Protocol section 19 and `protocol/conformance/atomic-fact-vectors.json` describe
this optional bridge without changing legacy vectors, event wire formats or
migrations. Strictly validating the bridge after capture sanitization is the
hosted caller's responsibility; redaction still takes precedence over storing
secrets. Converter roundtrip fidelity describes already-admitted content, not a
promise to preserve prohibited content through a storage sanitizer.

## Tests and observed result

Tests were written first. Before implementation,
`python -m pytest tests/test_atomic_fact_envelope.py -q` failed at collection
because both public converter APIs were absent. The implemented suite verifies
every AtomicFact field through JSON serialization, full confidence precision,
blank legacy timestamps/revisions, custom legacy categories, independent nested
copies, duplicate/original scope order, conflicting tenants and dimensions,
malformed/unknown fields, unsupported markers, mirror tampering, lifecycle
admission and effective expiry boundaries. A real temporary SQLite journal test
proves expiry hides current reads, does not enqueue erasure, and leaves the
original payload available in history.

```bash
./.venv/bin/python -m pytest tests/test_atomic_fact_envelope.py \
  tests/test_memory_record.py tests/test_memory_journal.py \
  tests/test_fact_evidence.py -q
```

Observed final rerun: **167 passed in 13.58 seconds**. This is a wire and policy regression
measurement, not a fact-write latency, hosted throughput, extraction-quality or
answer-accuracy gate. Hosted authorization, evidence currency and contradiction
admission require separate integration evidence.

The separate legacy/optional conformance regression command
`./.venv/bin/python -m pytest tests/test_conformance.py tests/test_conformance_extensions.py tests/test_retrieval_policy_vectors.py -q`
reported **54 passed, 1 skipped in 4.24 seconds**. Ruff, compilation, docs drift
and diff-whitespace checks passed. Standalone strict mypy of this module reports
eight findings in the existing preference helpers below the converter section;
it reports none in the new bridge. This module is not in the repository's
required strict-mypy list, and those unrelated preference helpers were left
unchanged.
