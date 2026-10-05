# Preparation, evidence chains and portable memory

These changes follow the implementation/paper comparisons recorded in
`competitor-analysis.md`. They improve CommonTrace's production paths without
changing official judges or relying on remote model inference. They do not
establish superiority over competitors on answer accuracy.

## Reusable ingestion work

Mem0 embeds during ingestion; Hindsight's retain phase constructs its recall
representations before questions arrive. MemGPT and the sleep-time paper explain
why reusable processing belongs outside latency-sensitive queries. CommonTrace
previously let its first semantic query encode every missing passage.

`commontrace conversation index SPACE --model arctic-m` now streams local
embeddings into the shared content-hash cache. It supports selected sessions,
reuses completed batches after interruption, and encodes only missing content.
It does not require an LLM or create lossy summaries. Within a process,
request-scoped connections share cache-miss coordination and build one compact
index at a time per space/model. Warm scoring stays parallel. Weak references
release coordination locks after their callers finish.

Baseline: `de1c2878ab5700e84c9ca60ed2e0fd964881540a`. Raw environment, settings
and measurements are in `memory-preparation-results.json`.

| Check | Before | After |
|---|---:|---:|
| Corpus rows encoded by four overlapping requests, 2,048 passages | 8,192 | 2,048 |
| Encoder batches in that workload | 8 | 2 |
| Real Arctic CPU first recall, 260 passages, excluding model loading | 9,308.981 ms | 101.353 ms |
| Explicit preparation before that first recall | none | 9,340.129 ms |
| Retrieved estimated tokens in that real-model check | 1,404 | 1,404 |

All four synthetic requests returned identical top-ten ids across both versions.
The synthetic encoder has an artificial 20 ms delay per batch; its latency is
not a model-speed measurement. The real-model check used cached Arctic weights,
`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, CPU inference, and one thread for
OpenMP/OpenBLAS. Both versions recovered the intermediate entity's award and
the historical residence. Preparation shifts cost out of the query; the combined
preparation/query cost was slightly higher in this single small-corpus run.
Model loading is recorded separately and remains a cold-start cost.

Reproduce with independent clean checkouts; no API key is required:

```bash
OPENBLAS_NUM_THREADS=1 python research/profile_memory_preparation.py BASELINE_CHECKOUT
OPENBLAS_NUM_THREADS=1 python research/profile_memory_preparation.py CANDIDATE_CHECKOUT
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  python research/profile_local_conversation.py BASELINE_CHECKOUT
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  python research/profile_local_conversation.py CANDIDATE_CHECKOUT --prepare
```

The real-model commands require the attention extra and already cached model
weights. They perform no generation or judging. Model quality, a full 10M-token
run, and multi-process miss coordination remain unmeasured.

## Evidence chains under a budget

Hindsight's multi-arm fusion exposes a common failure: a unique useful result
can disappear when other arms dominate the cutoff. Graphiti and Cognee traverse
bounded neighborhoods, but connected passages must still reach the answerer.
CommonTrace previously admitted only four graph discoveries, dropping later
discoveries entirely, and reranking could separate an answer from its support.

All discoveries now reach the candidate pool, retaining the existing 64-path
and two-hop bounds. When a graph passage is selected, its connecting passages
are selected together, at most two ancestors. Their combined text and session
headers must fit the caller's budget; every ancestor passes the source,
metadata and injection checks. A tiny budget cannot fall back to an isolated
graph answer. `selected_graph_paths` distinguishes discovered paths from chains
whose evidence was actually included. No new model call is required.

Regression checks cover a terminal passage reranked above its two cross-session
ancestors, discoveries beyond the four-position quota, insufficient budgets,
and disallowed/unsafe/self-referential support. Entity co-occurrence remains
evidence discovery, not proof of a semantic relationship.

## Restorable evidence and beliefs

Zep's episode-backed edges and Honcho's premise tracking make source retention
essential. CommonTrace's previous JSONL archive exported messages and summary
text but silently lost manually/model-derived memories and extraction progress.

Version 2 archives preserve exact rule/manual/model facts, owners, slots,
timestamps, provenance, insertion order across sessions, summary metadata and
extraction checkpoints. Import remaps every source id into the destination;
reversions at identical timestamps survive. Reimport matches individual fact
occurrences and adds no duplicates. All sessions commit atomically; malformed
later JSON or unsupported provenance rolls back earlier writes. Nested write
operations use savepoints. Legacy archives remain readable.

CLI import reads one JSONL session at a time, and export writes one session at a
time from one consistent snapshot. File export publishes a completed temporary
file atomically, preserving the previous backup if export fails. A session line
still materializes in memory; pending fact metadata remains until commit.
Restoring into a longer existing session does not install an incomplete summary
or skip unprocessed messages by advancing its checkpoint. Restored premise
deletion invalidates supported facts through the existing lifecycle rules.

On a synthetic 300-session, 6,000-message archive, streaming reduced the peak
Python allocation measured by `tracemalloc` from 12,932,511 to 1,423,197 bytes
for export and from 13,841,396 to 268,534 bytes for import. This excludes native
SQLite allocations and is not total RSS. The richer archive grew from 3,873,990
to 4,013,283 bytes. Under allocation tracing, export took 0.150 versus 0.194
seconds and import 2.845 versus 3.118 seconds: lower allocation peaks and better
correctness come with additional CPU work. Reproduce with
`python research/profile_memory_archives.py CHECKOUT` against each revision.

Validation: focused memory/portability checks, complete core/end-to-end suite,
Ruff, diff whitespace checks, and the existing Bandit medium/high gate. GitHub CI
checks the published commit; execution results are reported with that revision.
