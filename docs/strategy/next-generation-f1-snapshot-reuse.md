# F1: Reuse the detached fact snapshot

`_add_locked` needs a serialized fact payload to compute its revision, and the SQLite `FactMap` needs the same payload to stage the fact and event projection. Reusing one detached snapshot removes a second serialization for each new fact. The built-in `StatementIndex` exposes this through `add_with_snapshot`; `_index_add` only selects that path for the built-in `add` implementation. Subclasses overriding the established `add(self, fact)` method keep the one-argument call. `FactMap.stage` checks that a supplied snapshot matches the fact ID and statement before writing.

The regression tests cover one `to_dict` call per new fact, equality of event and SQLite payloads, the FTS projection, a subclass using the legacy method signature, and existing rollback behavior.

At a 10K store, a cProfile diagnostic around a 1K `add_facts` batch reduced `AtomicFact.to_dict` calls from 2,000 to 1,000 and cumulative time from 0.05374s to 0.02922s. A same-HEAD 30K ABBA diagnostic measured a baseline median of 2,176.47 facts/s and candidate median of 2,289.89 facts/s (+5.21%); each candidate round exceeded each baseline round.

The exact 1M acceptance workload then measured **1.797 ms single-write p95** and **2,071.287 facts/s** for its 1,000-fact batch, clearing the `<20 ms` and `>=2,000 facts/s` gates. The report records 20 single-write trials; batch throughput is one 1,000-fact batch. This is a narrow 3.6% margin on the local synthetic independent-journal workload. No same-HEAD 1M baseline was run, so the result establishes a measured candidate pass, not a controlled causal gain. Hybrid quality/latency, server behavior, embeddings, and real-world workload performance remain outside this report.

The exact runner, source hashes, round data, and limitations are in the [30K diagnostic](measurements/f1-snapshot-reuse-diagnostic.json) and the [1M candidate report](measurements/f1-snapshot-reuse-candidate-1m.json); both JSON files have adjacent SHA-256 manifests.

A later 1M run on the active descendant measured **1.766 ms single-write p95**
and **1,995.368 facts/s** for the 1,000-fact batch. The single-write target
passed and the batch target missed by 4.632 facts/s. Its source hashes match
the launch-time snapshot and the run completed without competing retrieval
tests. This is not an exact-source replication of the earlier 2,071.287
facts/s report: `configuration.py` and `memory_record.py` had different hashes
in that earlier report. The two results therefore show a marginal, unstable
batch rate around the threshold; they establish neither a reproducible pass
nor a causal gain. The measured report and SHA-256 manifest are in
[the follow-up artifact](measurements/f1-snapshot-reuse-followup-2026-10-12.json).
