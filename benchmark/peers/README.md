# Peer retrieval benchmark

How well CommonTrace finds the right memory, against the retrieval stacks
other agent-memory products ship, on the two public benchmarks those
products publish results on.

Reproduce everything below:

```bash
pip install -e ".[attention]" rank-bm25 chromadb "mem0ai[nlp,extras]"
python -m spacy download en_core_web_sm            # mem0's lemmatizer
python benchmark/peers/peerbench.py --download      # LoCoMo + LongMemEval
python benchmark/peers/peerbench.py --dataset locomo \
    --systems ct-lexical,ct-lexical:idf-v3,ct-semantic,ct-fusion,ct-fusion-v3,bm25,dense-minilm,hybrid,chroma,mem0
python benchmark/peers/peerbench.py --dataset locomo \
    --systems ct-lexical-rerank,ct-fusion-rerank,ct-fusion-v3-rerank,mem0-rerank
python benchmark/peers/peerbench.py --dataset longmemeval --per-type 10 \
    --systems ct-lexical,ct-lexical:idf-v3,ct-semantic,ct-fusion,ct-fusion-v3,bm25,dense-minilm,hybrid
# Reranked and gated systems; append -fast for the fast model (ct-fusion-rerank-fast, ...)
python benchmark/peers/peerbench.py --dataset locomo --systems ct-gated-fast,ct-gated
python benchmark/peers/peerbench.py --dataset longmemeval --per-type 10 \
    --systems ct-lexical-rerank,ct-fusion-rerank,ct-fusion-v3-rerank
```

Raw results, including per-category breakdowns, are in `results/`.

## What is measured, and what is not

**Retrieval, scored from the datasets' own evidence labels.** No LLM runs
anywhere, and no judge grades anything, so every number is reproducible on a
laptop and says only one thing: did the memory that answers the question
come back, and how high?

- **LoCoMo** (Maharana et al., ACL 2024): 10 long conversations, 5,882
  dialogue turns. Each question names the turns that answer it. A system
  indexes one conversation's turns and is asked each of its questions.
  1,531 questions; the adversarial category (no answer exists) is excluded,
  as are 9 questions whose evidence ids do not resolve.
- **LongMemEval** (Wu et al., ICLR 2025), the `S` variant: each question has
  its own haystack of about 50 chat sessions (about 500 turns), and names
  the sessions that answer it. A system indexes turns, and a session ranks
  at its best turn (the paper's session-level metric). 60 questions, 10 per
  question type, sampled with a fixed seed. Abstention questions are
  excluded. The subset exists for compute: embedding every haystack of all
  500 questions is about 235,000 turns per dense system on a CPU.

Metrics: recall@k (the fraction of answering turns or sessions in the top
k), NDCG@10, and MRR.

**What this is not.** Products in this space publish LoCoMo and
LongMemEval results as end-to-end *question-answering accuracy*, graded by
an LLM judge over an LLM's answer. Those numbers mix retrieval, the answering
model and the judge's prompt, and they are not comparable with anything
here. Retrieval is the part a memory layer owns.

**Every system at its shipped defaults.** CommonTrace keeps its relevance
floor; mem0 keeps its 0.1 score threshold. A system that returns fewer than
k results is scored on what it returned.

## Systems

| System | What runs |
|---|---|
| commontrace lexical (idf-v2) | `rank_lessons` exactly as `retrieve`/`query` run it, default scorer |
| commontrace lexical (idf-v3) | the same, opt-in stemmed scorer |
| commontrace semantic | the semantic arm's model (`multi-qa-mpnet-base-dot-v1`), exact cosine |
| commontrace fusion | lexical + semantic, fused by rank (RRF, k=60), as `--fusion rrf` does: each arm contributes the requested number of results |
| commontrace gated fusion | both arms hand over 30 candidates (the lexical arm without its floor); the reranker orders them, and a candidate that did not clear the lexical floor reaches the page only if the cross-encoder scores it at least -4, as `--fusion gated` does. The default where the attention extra is installed, with the fast model |
| commontrace + rerank | the first stage hands its top 30 to `commontrace/rerank_arm.py`, which reorders them with a cross-encoder, as `--rerank` does: `ms-marco-MiniLM-L-6-v2` (`cross-encoder`) or `ms-marco-TinyBERT-L-2-v2` (`cross-encoder-fast`) |
| BM25 (Okapi) | `rank_bm25`; the keyword arm of the hybrid search Graphiti/Zep, Hindsight and mem0 describe |
| dense MiniLM | `all-MiniLM-L6-v2`, exact cosine; the default local embedding in Chroma, mem0's HF provider and LlamaIndex examples |
| hybrid BM25 + MiniLM | the two fused the same way: the keyword + vector pattern Graphiti/Zep describe |
| Chroma | `chromadb` 1.5.9, in-process, default embedding function (ONNX MiniLM, HNSW) |
| mem0 | `mem0ai` 2.2.0 with `infer=False` (memories stored verbatim, no LLM): its hybrid search -- MiniLM vectors, lemmatized BM25, entity boosts -- on local Qdrant |
| mem0 + rerank | the same, with mem0's own `sentence_transformer` reranker on the same cross-encoder, `rerank=True`. mem0 reorders the results it would return; it does not fetch a deeper pool |

**Not run**, because retrieving at all requires an LLM call to build or
query their memory (no API keys were used): Zep/Graphiti, Letta, Hindsight,
Honcho, Cognee, LangMem, Memobase, memU, EverOS, Memary, Memoripy,
Supermemory (hosted), and the commercial SDKs. Their retrieval layers are
built from the same parts measured above: BM25, dense vectors and hybrids of
the two.

## LoCoMo: 1,531 questions, turn-level

| System | R@5 | R@10 | NDCG@10 | MRR |
|---|---:|---:|---:|---:|
| **commontrace fusion (idf-v3) + rerank** | **0.676** | **0.734** | **0.627** | **0.629** |
| commontrace fusion (idf-v2) + rerank | 0.670 | 0.726 | 0.622 | 0.626 |
| commontrace gated fusion, accurate model | 0.679 | 0.731 | 0.627 | 0.630 |
| mem0 2.x + its rerank (same model) | 0.612 | 0.661 | 0.569 | 0.576 |
| commontrace fusion (idf-v3) + fast rerank | 0.614 | 0.703 | 0.561 | 0.547 |
| **commontrace gated fusion, fast model** *(default with the attention extra)* | **0.598** | **0.669** | **0.545** | **0.537** |
| commontrace fusion (idf-v2) + fast rerank | 0.606 | 0.696 | 0.557 | 0.545 |
| commontrace lexical (idf-v2) + rerank | 0.597 | 0.624 | 0.562 | 0.577 |
| commontrace lexical (idf-v2) + fast rerank | 0.562 | 0.615 | 0.518 | 0.518 |
| commontrace fusion (idf-v3 + semantic) | 0.568 | 0.660 | 0.486 | 0.464 |
| commontrace fusion (idf-v2 + semantic) | 0.531 | 0.645 | 0.464 | 0.440 |
| mem0 2.x hybrid | 0.543 | 0.625 | 0.466 | 0.446 |
| hybrid BM25 + MiniLM | 0.499 | 0.615 | 0.433 | 0.407 |
| commontrace semantic | 0.460 | 0.561 | 0.402 | 0.383 |
| commontrace lexical (idf-v3) | 0.492 | 0.558 | 0.430 | 0.412 |
| commontrace lexical (idf-v2) | 0.472 | 0.540 | 0.406 | 0.387 |
| BM25 (Okapi) | 0.466 | 0.539 | 0.408 | 0.392 |
| dense MiniLM | 0.388 | 0.490 | 0.336 | 0.314 |
| Chroma (default) | 0.388 | 0.488 | 0.335 | 0.314 |

Recall@10 by question category:

| System | single-hop | temporal | multi-hop | open-domain |
|---|---:|---:|---:|---:|
| commontrace fusion (idf-v3) + rerank | **0.833** | 0.783 | **0.475** | **0.445** |
| commontrace fusion (idf-v2) + rerank | 0.821 | **0.785** | 0.467 | 0.441 |
| commontrace gated fusion, accurate | 0.826 | 0.806 | 0.473 | 0.371 |
| commontrace gated fusion, fast *(default)* | 0.765 | 0.755 | 0.393 | 0.332 |
| mem0 2.x + its rerank | 0.730 | 0.735 | 0.457 | 0.383 |
| commontrace fusion (idf-v3) | 0.764 | 0.723 | 0.371 | 0.373 |
| commontrace fusion (idf-v2) | 0.754 | 0.712 | 0.340 | 0.351 |
| mem0 2.x hybrid | 0.694 | 0.723 | 0.396 | 0.348 |
| hybrid BM25 + MiniLM | 0.718 | 0.686 | 0.318 | 0.319 |
| BM25 (Okapi) | 0.643 | 0.625 | 0.215 | 0.277 |

## LongMemEval: 60 questions, session-level

| System | R@5 | R@10 | NDCG@10 | MRR |
|---|---:|---:|---:|---:|
| **commontrace fusion (idf-v3) + rerank** | **0.963** | **0.992** | **0.925** | 0.910 |
| commontrace fusion (idf-v3) + fast rerank | 0.960 | 0.983 | 0.903 | 0.881 |
| dense MiniLM | 0.954 | 0.961 | 0.910 | 0.910 |
| hybrid BM25 + MiniLM | 0.948 | 0.963 | 0.921 | **0.920** |
| commontrace fusion (idf-v2) + rerank | 0.930 | 0.967 | 0.909 | 0.900 |
| commontrace lexical (idf-v2) + rerank | 0.930 | 0.967 | 0.908 | 0.899 |
| commontrace fusion (idf-v2) + fast rerank | 0.927 | 0.967 | 0.896 | 0.878 |
| commontrace lexical (idf-v3) | 0.926 | 0.940 | 0.869 | 0.874 |
| commontrace gated fusion, fast *(default with the attention extra)*, and lexical + fast rerank | 0.923 | 0.947 | 0.894 | 0.884 |
| commontrace fusion (idf-v2 + semantic) | 0.914 | 0.952 | 0.882 | 0.876 |
| commontrace fusion (idf-v3 + semantic) | 0.910 | 0.988 | 0.879 | 0.851 |
| commontrace semantic | 0.907 | 0.936 | 0.852 | 0.847 |
| commontrace lexical (idf-v2) | 0.852 | 0.940 | 0.849 | 0.832 |
| BM25 (Okapi) | 0.825 | 0.907 | 0.813 | 0.816 |

Sixty questions is a small sample: one question moves R@5 by up to 1.7
points, so differences of a point or two here are not findings. What does
hold: every system clears 0.9 at R@10, stemming and reranking each help,
and on this dataset MiniLM embeds conversational turns better than the
semantic arm's mpnet (0.954 vs 0.907 R@5). With the default lexical arm,
CommonTrace's reranked fusion trails dense MiniLM on R@5 (0.930 vs 0.954);
with the stemmed arm it leads R@5, R@10 and NDCG, and the BM25 + MiniLM
hybrid keeps MRR by a hundredth. mem0 was not run on LongMemEval: its
per-memory ingestion (55 ms each) over 29,204 turns was out of this run's
compute budget.

## Speed

Query latency, p50 on LoCoMo (conversations of about 600 memories), on a
4-core CPU container. The commontrace and mem0 rows were measured together,
alone on the machine; the BM25, MiniLM and Chroma rows come from the
earlier run, when benchmarks shared it, so read them as upper bounds.

| System | query p50 | index cost per memory | LoCoMo R@5 |
|---|---:|---:|---:|
| commontrace lexical | 0.4 ms | 0.04 ms | 0.472 |
| BM25 (`rank_bm25`) | 0.8 ms | 0.04 ms | 0.466 |
| dense MiniLM (exact) | 9 ms | 4.5 ms (embedding) | 0.388 |
| commontrace lexical + fast rerank | 30 ms | 0.04 ms | 0.562 |
| commontrace gated fusion, fast *(default with the extra)* | 165 ms | as fusion | 0.598 |
| commontrace fusion | 37 ms | 39.5 ms (mpnet embedding, once per lesson; the index refreshes itself) | 0.531 |
| mem0 2.x | 56 ms | 38–55 ms (embedding, lemmatizing, entity extraction per add) | 0.543 |
| commontrace fusion + fast rerank | 75 ms | as fusion | 0.606 |
| commontrace lexical + rerank | 224 ms | 0.04 ms | 0.597 |
| Chroma (in-process) | 265 ms | 43 ms (ONNX embedding + HNSW) | 0.388 |
| commontrace fusion + rerank | 313 ms | as fusion | 0.670 |

The reranker's cost is the cross-encoder reading 30 (task, lesson) pairs.
The fast model does that in about 30 ms, which puts lexical retrieval with
fast reranking ahead of mem0 on R@5, NDCG and MRR at about half its query
latency, with no embedding index to build. mem0 with its own reranker on the
accurate model ran at a comparable 486 ms p50 on the shared machine.

The semantic arm's cost is the query embedding by a 110M-parameter model on
a CPU (about 35 ms). The lexical arm is under a millisecond: at 6,400 lessons a full local
retrieval, including checking every lesson file for changes, takes about
44 ms (`commontrace/reference/measure_local_latency.py`; CI fails the build
above 500 ms).

## What the numbers say

- **Out of the box, CommonTrace now beats mem0 on every aggregate metric on
  LoCoMo.** Where the attention extra is installed, a new store uses gated
  fusion with the fast reranker: R@5 0.598, R@10 0.669, NDCG 0.545, MRR
  0.537, against mem0's 0.543, 0.625, 0.466 and 0.446. It leads on
  single-hop and temporal questions; mem0 keeps multi-hop (0.396 vs 0.393)
  and open-domain (0.348 vs 0.332) by a small margin. With the accurate
  reranker, gated fusion leads every category and is the most accurate
  configuration measured (R@5 0.679, MRR 0.630).
- **Gated fusion is the one fused ranking that passes the curated-store
  gates.** Plain fusion fills every slot on the page with whatever the
  semantic arm ranked, which puts unrelated lessons into every experiment.
  Gated fusion admits a lesson that did not clear the relevance floor only
  when the cross-encoder vouches for it (score at least -4). On the curated
  fixture unrelated lessons score -8 to -11, so every field keeps exactly
  its recall and collateral; on LoCoMo the semantic arm's real finds get
  through.
- **On LongMemEval the default does not move.** Every question already has
  30 floor-cleared lexical candidates, and the fast cross-encoder scores
  long conversational turns low, so no semantic candidate is vouched for;
  the default stays at R@5 0.923, behind dense MiniLM's 0.954.

- **On LoCoMo, with reranking, CommonTrace leads every system measured, on
  every metric and in every question category.** Fused retrieval reranked by a small
  cross-encoder puts an answering turn in the top 5 for 67.0% of questions
  (67.6% with the stemmed arm), against 54.3% for mem0's hybrid search, and
  its MRR is 0.626 against 0.446. The reranker closes the one gap fusion
  alone left: multi-hop questions, whose evidence spans several turns,
  where mem0's entity boosts had led (0.396); reranked fusion reaches 0.467.
- **The lead is not the reranker's alone.** Given the same cross-encoder
  through its own `rerank=True`, mem0 improves to 0.612 R@5 and 0.661 R@10,
  and CommonTrace's reranked fusion is still ahead on every metric and every
  category. The difference is the pool: mem0 reorders the results it would
  have returned, while CommonTrace hands the reranker a deeper fused pool.
- **Reranking helps because the pool is good.** The cross-encoder can only
  reorder what the first stage found. Over the lexical arm alone it lifts
  R@5 from 0.472 to 0.597, but R@10 stays at 0.624, because the answer is
  not in a keyword pool it cannot see past. Over the fused pool, R@10 rises
  to 0.726.
- **The fast reranker is the best value.** Over the lexical arm it runs in
  about 30 ms, half of mem0's query time, needs no embedding index, and
  beats mem0 on R@5, NDCG and MRR. Over the fused pool, at 75 ms, it beats
  mem0 on every aggregate metric (R@5 0.606 vs 0.543, R@10 0.696 vs 0.625).
- **On LongMemEval the field is closer.** Every system clears 0.9 recall at
  10 on this 60-question subset. Reranked fusion with the stemmed arm leads
  R@5, R@10 and NDCG; with the default arm it trails dense MiniLM on R@5.
- **Without reranking, fusion is still ahead on LoCoMo.** With the stemmed lexical arm
  it beats mem0's hybrid search on every aggregate metric; with the default
  arm it finds more answers in the top 10 (0.645 vs 0.625) and ranks them
  slightly lower.
- **CommonTrace's default lexical retriever matches or beats BM25** on both
  datasets and is faster per query. On LongMemEval it leads BM25 by 2.7
  points of R@5 and 3.3 of R@10.
- **Stemming (`idf-v3`) is worth it on free text.** It adds 7.4 points of
  session recall@5 on LongMemEval, which played no part in choosing its
  floor. It stays opt-in because, on the curated lesson fixture, one of eight
  fields (clinical) retrieves more collateral under it (see
  `commontrace/retrieval.py`, `IDF_V3_FLOOR`).
- **Fusion widens the pool.** On LoCoMo every hybrid beats both of its arms.
  On LongMemEval, where one arm is already near the ceiling, fusion raises
  R@10 but not always R@5, and reranking is what turns the wider pool into
  a better top 5. Agents get both: `retrieve` over MCP runs the same fused,
  reranked ranking as `commontrace query` when a store opts in.
- **The embedding model matters more than the vector store.** Chroma and
  exact-cosine MiniLM are the same to three decimals. Which model is better
  depends on the data: mpnet is 7 points of R@10 above MiniLM on LoCoMo's
  short turns, and MiniLM is ahead on LongMemEval's long ones.

What none of these systems measures, and CommonTrace does, is whether a
retrieved memory changed the outcome of the task it was retrieved for
(README §9). Retrieval quality is the precondition for that, not a
substitute for it.
