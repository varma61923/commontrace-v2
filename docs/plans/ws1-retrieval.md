# WS1: Retrieval to state of the art

**Problem.** Retrieval misses evidence on multi-hop, preference and summary questions, and the lexical arm cannot match paraphrases.

**Design.** Multi-signal fusion with named recipes (`search_recipes.py`); dense embeddings and named second-stage rerankers (`providers.reranker`); query planning and entity bridging (`query_plan.py`); a context budget sized by the question's shape (`conversation.search.budget_for`, `--budget auto`); a store abstraction with pgvector HNSW, LanceDB and graph backends (`store.py`).

**Invariants touched.** Source identity: every delivered passage keeps its source turn. Exact fallbacks behind every ANN index. Budgets are floors the caller sets; adaptive spend is reported in `explain.budget`.

**Tests.** `tests/test_conversation*.py`, `tests/test_memory_evolution.py` (recipes), `tests/test_store*.py`, `tests/test_bridge_turns.py`.

**Benchmark to move.** Target A (evidence and accuracy on LoCoMo / LongMemEval / BEAM), target C (>= 90% evidence within 1,000 tokens on LongMemEval-S; measured 79.60% at 1,500 lexical, 83.02% adaptive) and target D (1M-memory latency; not yet measured).

**Risks.** Dense retrieval costs CPU at ingest; the cross-encoder adds about 480 ms p50 on a 4-core CPU. Bridging turns lowered LongMemEval evidence (-0.74), so it stays opt-in.

**Rollback.** Every retrieval improvement is a named recipe or flag; the default recipe is unchanged unless a measured run justifies the change.
