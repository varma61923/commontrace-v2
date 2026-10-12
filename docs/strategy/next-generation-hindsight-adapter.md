# Hindsight raw-source benchmark profile

The optional `hindsight-raw` profile adapts Hindsight's public embedded client
to the existing conversation benchmark. It calls `retain` and `recall`, binds
each source turn to a document ID, applies history cutoffs before ingestion,
and packs only whole turns from the benchmark corpus. It does not pass gold
answers or evidence into the vendor.

The local Hindsight snapshot at `a9eeed77f9bce99d8f97d39a30d3281747300824`
documents that `HINDSIGHT_API_LLM_PROVIDER=none` makes retain store chunks
without fact extraction while recall continues with semantic, BM25 and graph
retrieval (`hindsight-docs/docs/developer/configuration.mdx`, no-LLM section).
The adapter uses this chunks-only path, local `BAAI/bge-small-en-v1.5`
embeddings, Hindsight's native recall ranking, and disabled reranking. It binds
package source and model artifact hashes into the benchmark descriptor. A
temporary `HOME` isolates the embedded PostgreSQL `pg0` files, and offline
model flags make a missing cached model fail closed instead of downloading it.

The adapter's contract tests pass, but **no native Hindsight score was
measured**. This environment has no `hindsight-api` executable, Hindsight
packages, `sentence-transformers`, or cached embedding artifact, so the profile
refuses to start. Install those local dependencies and cache the declared
embedding artifact before running the same harness; report its descriptor and
results alongside CommonTrace, mem0 and Graphiti. The profile is available via
`--memory-adapter hindsight-raw`.

Focused verification:

```bash
pytest -q tests/test_benchmark_vendor_profiles.py tests/test_benchmark_context_budget.py
```

Result: 28 passed, 3 skipped. The skip set includes the opt-in native Graphiti
profile; the Hindsight test uses a fake embedded client and verifies the public
call contract, source mapping, cutoff, offline settings, and environment
restoration.
