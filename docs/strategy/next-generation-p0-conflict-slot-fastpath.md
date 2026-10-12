# P0 sparse conflict-slot fast path

`_detect_conflicts()` needs candidate lookups only when another active fact
shares the incoming fact's lexical contradiction slot. `FactStore.StatementIndex`
now checks that condition through the existing `fact_slot` index before
materializing the opposite-negation and numeric-variant buckets. If no other
active row shares the slot, both candidate sets must be empty and conflict
recording can return early. Scope, negation, value-variant and batch ordering
checks remain in the original detector whenever a peer exists.

The fast path is disabled for LLM conflict judging because that mode also
searches semantic candidates outside the lexical slot. Conflict mode `off` and
slots shorter than two terms still exit before the new query. The regression
test instruments SQLite statements for a batch of distinct slots and verifies
that no candidate-bucket queries run; the existing conflict, deduplication and
temporal tests cover repeated slots and LLM judgments.

The exact one-million-fact command and source hashes are recorded in
[the measurement JSON](measurements/p0-batch-conflict-slot-candidate.json).
The candidate measured 6.127 ms single-write p95 and 1,962.476 facts/s for a
1,000-fact batch. Single-write passes `<20 ms`; batched ingest still misses the
unchanged `>=2,000 facts/s` target by 37.524 facts/s. A prior 1M candidate
measured 1,819 facts/s, but the runs were not controlled pairs and do not
establish a causal gain. The separate 100K same-harness run measured 2,120.9
versus 2,064.4 facts/s. Keep all gate outcomes explicit.

Focused validation on the integrated source:

```sh
env -u COMMONTRACE_ROOT -u JUSTDOIT_ROOT \
  /tmp/commontrace-goal-env/bin/python -m pytest \
  tests/test_fact_store.py tests/test_fact_conflicts.py tests/test_fact_dedup.py -q
# 98 passed
```

Ruff and `git diff --check` pass. The million-fact candidate run takes about
eight and a half minutes to seed its synthetic store; it is reproducible with
the command saved in the JSON report.
