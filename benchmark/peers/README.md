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
| commontrace + rerank | the first stage hands its top 30 to `commontrace/rerank_arm.py`, which reorders them with a cross-encoder (`ms-marco-MiniLM-L-6-v2`), as `--rerank cross-encoder` does |
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
| commontrace lexical (idf-v2) + rerank | 0.597 | 0.624 | 0.562 | 0.577 |
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
| commontrace fusion (idf-v3) | 0.764 | 0.723 | 0.371 | 0.373 |
| commontrace fusion (idf-v2) | 0.754 | 0.712 | 0.340 | 0.351 |
| mem0 2.x hybrid | 0.694 | 0.723 | 0.396 | 0.348 |
| hybrid BM25 + MiniLM | 0.718 | 0.686 | 0.318 | 0.319 |
| BM25 (Okapi) | 0.643 | 0.625 | 0.215 | 0.277 |

## LongMemEval: 60 questions, session-level

| System | R@5 | R@10 | NDCG@10 | MRR |
|---|---:|---:|---:|---:|
| commontrace lexical (idf-v3) | 0.926 | 0.940 | 0.869 | 0.874 |
| commontrace lexical (idf-v2) | 0.852 | 0.940 | 0.849 | 0.832 |
| BM25 (Okapi) | 0.825 | 0.907 | 0.813 | 0.816 |

The dense, hybrid and fused systems are not in this table yet. Embedding the
subset's 29,204 turns takes each embedding model over an hour on this CPU,
and those runs are still in progress; `results/` will carry them. The LoCoMo
tables above cover every system.

## Speed

Query latency, p50 on LoCoMo (a conversation of about 600 memories), on a
shared 4-core CPU container. Treat absolute numbers as indicative: the runs
shared the machine with each other.

| System | query p50 | index cost per memory |
|---|---:|---:|
| commontrace lexical | 0.5 ms | 0.04 ms |
| BM25 (`rank_bm25`) | 0.8 ms | 0.04 ms |
| dense MiniLM (exact) | 9 ms | 4.5 ms (embedding) |
| mem0 2.x | 58 ms | 55 ms (embedding, lemmatizing, entity extraction per add) |
| commontrace fusion | 120–140 ms | 39.5 ms (mpnet embedding, once per lesson; the index refreshes itself) |
| Chroma (in-process) | 265 ms | 43 ms (ONNX embedding + HNSW) |

The semantic arm's cost is the query embedding by a 110M-parameter model on
a CPU. The lexical arm is under a millisecond: at 6,400 lessons a full local
retrieval, including checking every lesson file for changes, takes about
44 ms (`commontrace/reference/measure_local_latency.py`; CI fails the build
above 500 ms).

## What the numbers say

- **With reranking, CommonTrace leads every system measured, on every
  metric and every LoCoMo category.** Fused retrieval reranked by a small
  cross-encoder puts an answering turn in the top 5 for 67.0% of questions
  (67.6% with the stemmed arm), against 54.3% for mem0's hybrid search, and
  its MRR is 0.626 against 0.446. The reranker closes the one gap fusion
  alone left: multi-hop questions, whose evidence spans several turns,
  where mem0's entity boosts had led (0.396); reranked fusion reaches 0.467.
- **Reranking helps because the pool is good.** The cross-encoder can only
  reorder what the first stage found. Over the lexical arm alone it lifts
  R@5 from 0.472 to 0.597, but R@10 stays at 0.624, because the answer is
  not in a keyword pool it cannot see past. Over the fused pool, R@10 rises
  to 0.726.
- **Without reranking, fusion is still ahead.** With the stemmed lexical arm
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
- **Fusion is where the gain is.** Every hybrid beats both of its arms. That
  is why agents now get it too: `retrieve` over MCP runs the same fused
  ranking as `commontrace query` when a store opts in.
- **The embedding model matters more than the vector store.** Chroma and
  exact-cosine MiniLM are the same to three decimals; mpnet is 7 points of
  R@10 above MiniLM on LoCoMo.

What none of these systems measures, and CommonTrace does, is whether a
retrieved memory changed the outcome of the task it was retrieved for
(README §9). Retrieval quality is the precondition for that, not a
substitute for it.
