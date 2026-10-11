# Staged-write evidence: throughput gate remains open

The latest production source is `cd8c1dc`. Every new million-copy diagnostic
misses the unchanged 2,000 facts/s batch target. No exact-1M release rerun is
claimed: that gate still records `aeadf5d`, 3.86 ms single-write p95 and
1,167 facts/s batched, with benchmark exit 1. P0 and the overall P0–P4 objective
remain incomplete.

`ce1abe2` keeps metadata and FTS current during admission, overlays pending
near candidates, then batches shared-term frequencies, postings and validated
events. It covers semantic candidates beyond the first 100 pending facts,
strongest-match selection, reinforcement, rollback after partial event inserts,
rebuild and reverted edits. The strengthened legacy fixture drains pooled
handles before restoring old schemas; a serialized worker schedule reproduced
the original fixture defect on untouched `d50d54b`.

`64b64f4` adds transaction-owned pure lexical reuse, bounded to 4,096 entries
and 8 MiB of accounted retained weight. Abort/commit cleanup physically releases
entries; nested and concurrent writers have separate owners. This accounting
does not promise a process-RSS cap.

`cd8c1dc` reuses a detached staged payload only when exact immutable scalar
identities prove it unchanged. Post-stage edits, nested values and subclasses
retain the existing snapshot path. Final MemoryRecord validation still runs.
The profile reduces new-fact snapshot calls from 3,000 to 2,000 and JSON
encodings from 11,000 to 10,000 per 1,000 facts; these are call counts, not
proof of a throughput gain.

Each row below runs baseline/candidate/candidate/baseline on the same growing
private copy. The counter advances between runs; concurrent frozen full-suite
validation and some Graphify/scoped checks add load. These are diagnostic
observations, not controlled causal comparisons or release acceptance.

| Change | Frozen baseline → candidate | All four batch rates, facts/s |
| --- | --- | --- |
| Eager-FTS staging | `d50d54b` → `ce1abe2` | 1,177 / 1,601 / 1,645 / 1,236 |
| Pure lexical reuse | `ce1abe2` → `64b64f4` | 1,619 / 1,741 / 1,756 / 1,667 |
| Guarded staged snapshots | `64b64f4` → `cd8c1dc` | 1,730 / 1,722 / 1,653 / 1,129 |

All raw rounds, including the sharply slower last baseline, are retained in
[the diagnostic aggregate](measurements/p0-staged-write-diagnostics.json).
Each runner verifies frozen tracked-source hashes and import paths. Every
timed batch's 1,000 admitted facts is checked against canonical near-term sets
and the production near-match reader. Internal document-frequency equality
alone cannot prove projection completeness.

Tests failed before each runtime change: staging 2 failures/3 passes,
lexical reuse 7 failures, final corrected snapshot fixture 1 failure/8 passes.
The final snapshot source passes 73 focused and 149 broader write/evidence/
record checks. Ruff, CI strict mypy, docs/OpenAPI drift, medium/high Bandit and
the installed dependency audit pass. The pinned 154-question LoCoMo retrieval
comparison passes at 1,500/4,000 tokens on both later candidates; this is not
model-judged answer accuracy. Graphify reports 21,526 nodes, 64,005 edges,
298 modules, 300 reachability candidates, zero dangling endpoints and zero
policy violations against unchanged 306/303 ceilings.

The frozen `d50d54b` suite passes 6,722 tests with 126 skips and 26 warnings in
4,145.44 seconds (observed exit 0). The later `cd8c1dc` suite is running;
their separate states are in [validation](measurements/p0-staged-write-validation.json).
A running suite is not a pass. The latest suite uses a dedicated environment whose
isolated child imports resolve to the same frozen checkout.

The validation artifact retains the diagnostic runner, reproduction commands,
test summaries and source bindings. Release acceptance still uses:

```sh
python -m benchmarks.next_generation_storage \
  --sizes 1000,10000,30000,1000000 --trials 20 --legacy-max 1000 \
  --output <report>
```

Verify the [signed manifest](measurements/p0-staged-write-signed.json) with the
existing pinned public key and artifact root. Its signature establishes
integrity, not scientific validity. Full local/server hybrid latency, restart
embedding counts, real reader/judge quality and causal SWE uplift remain open.
