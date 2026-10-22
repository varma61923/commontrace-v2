# Long-term memory benchmarks: LoCoMo, LongMemEval and BEAM

UniMem's conversation memory is measured end to end on the official data of the three
public long-term memory benchmarks. Every conversation is written through `Store.add`
exactly as an agent would write it; every question is answered from what `recall`
delivers, by a model, and graded by the benchmark's own judge protocol.

## What is measured

| Benchmark | Data (pinned) | Categories | Judge protocol |
|---|---|---|---|
| LoCoMo | `snap-research/locomo` `data/locomo10.json` @ `cbfbc1d` | single-hop, multi-hop, temporal, open-domain (adversarial category 5 excluded, as in Mem0/EverOS comparisons) | binary LLM judge used by Mem0/EverOS ([judges/locomo.py](../benchmarks/judges/locomo.py)) |
| LongMemEval-S (cleaned) | `xiaowu0162/longmemeval-cleaned` @ `98d7416` | single-session-user, single-session-assistant, single-session-preference, multi-session, knowledge-update, temporal-reasoning, and the abstention variants | official `get_anscheck_prompt` prompts, verbatim ([judges/longmemeval.py](../benchmarks/judges/longmemeval.py)) |
| BEAM | `Mohammadta/BEAM` @ `3205395` (100K, 500K, 1M) and `Mohammadta/BEAM-10M` @ `9b20961` | abstention, contradiction resolution, event ordering, information extraction, instruction following, knowledge update, multi-session reasoning, preference following, summarization, temporal reasoning | upstream rubric judge prompt verbatim; event ordering aligned by the upstream LLM equivalence prompt and reported as `tau_norm`, which is what upstream's report aggregates ([judges/beam.py](../benchmarks/judges/beam.py)) |

Two kinds of number are reported:

- **Retrieval** (no model involved, every question of the split): the share of gold
  evidence turns that reach the delivered context, and the tokens that context costs.
- **Answer accuracy** (a model answers from the delivered context, a model judges):
  the benchmark's headline metric, on a stratified sample when the model's quota does
  not allow every question.

## Reproduce

```bash
python scripts/fetch_memory_benchmarks.py --dest data/benchmarks --merge-beam-10m
pip install -e '.[fastembed]'   # CPU ONNX bge-small embeddings for the semantic arm

# Retrieval only, every question (no model, no key):
python benchmarks/conversation_bench.py --dataset locomo --data data/benchmarks/locomo10.json \
    --budget 6000 --adaptive-budget --max-budget 16000

# Answer accuracy with Gemma 4 31B on the Gemini Developer API as reader and judge:
export UNIMEM_LLM_PROVIDER=gemini UNIMEM_LLM_MODEL=gemma-4-31b-it UNIMEM_LLM_API_KEY=...
export UNIMEM_BENCH_INPUT_TPM=15000   # stay under the free tier's 16,000 input tokens/minute
python benchmarks/conversation_bench.py --dataset longmemeval --data data/benchmarks/longmemeval_s_cleaned.json \
    --limit 120 --budget 6000 --adaptive-budget --max-budget 16000 --answer \
    --answer-model gemma-4-31b-it --judge-model gemma-4-31b-it --answer-workers 16 --max-output-tokens 8192
```

Completed reader and judge calls are cached under `<root>/benchmark_cache`, so an
interrupted run resumes where it stopped. Each result file records the SHA-256 of the
dataset, the harness, the judges, the reader prompt and the product source, and a run
whose sources change while it is measuring is refused.

Gemma 4 thinks before it answers and bills that thinking as output; the 8,192-token
output cap matches `unimem.llm.GEMINI_MAX_TOKENS`. A lower cap makes some questions
fail every retry, and the harness refuses to drop a question rather than score only
the survivors.

## Results

Baseline is `main` before this change: a fixed 1,500-token recall and the previous
reader prompt. Improved is this change: an adaptive recall of 6,000 tokens that grows
to 16,000 for summaries, orderings, counts and lists (`answer`'s new default), and the
new reader prompt. Everything else (store, embeddings: CPU ONNX `bge-small-en-v1.5`,
judge code, generation settings) is identical between the two arms.

### Retrieval: gold evidence delivered to the reader

| Category | n | Baseline | Improved | Tokens |
|---|---:|---:|---:|---:|
| **LoCoMo (all 1,540)** | | | | |
| single-hop | 841 | 0.904 | 0.980 | 1447 → 6427 |
| multi-hop | 282 | 0.571 | 0.899 | 1444 → 8169 |
| temporal | 321 | 0.915 | 0.966 | 1451 → 5933 |
| open-domain | 96 | 0.531 | 0.779 | 1442 → 6695 |
| overall | 1540 | 0.823 | 0.950 | 1447 → 6660 |
| **LongMemEval-S (150, stratified)** | | | | |
| single-session-user | 23 | 0.935 | 1.000 | 1473 → 7231 |
| single-session-assistant | 25 | 1.000 | 1.000 | 1474 → 7942 |
| single-session-preference | 25 | 0.747 | 0.867 | 1473 → 7150 |
| knowledge-update | 22 | 0.818 | 1.000 | 1464 → 8935 |
| temporal-reasoning | 25 | 0.773 | 0.900 | 1467 → 10666 |
| multi-session | 23 | 0.625 | 0.935 | 1461 → 10578 |
| overall | 150 | 0.820 | 0.949 | 1468 → 8838 |
| **BEAM 100K (all 400)** | | | | |
| contradiction resolution | 40 | 0.815 | 0.853 | 1476 → 8201 |
| event ordering | 40 | 0.571 | 0.985 | 1452 → 8711 |
| information extraction | 40 | 0.692 | 0.792 | 1475 → 8570 |
| instruction following | 40 | 0.867 | 0.898 | 1471 → 6992 |
| knowledge update | 40 | 0.858 | 0.892 | 1472 → 7966 |
| multi-session reasoning | 40 | 0.641 | 0.867 | 1466 → 9820 |
| preference following | 40 | 0.735 | 0.833 | 1474 → 7539 |
| summarization | 40 | 0.303 | 0.586 | 1458 → 15874 |
| temporal reasoning | 40 | 0.988 | 1.000 | 1471 → 13726 |
| overall | 400 | 0.724 | 0.859 | 1469 → 9484 |

Abstention questions cite no evidence and have no retrieval score. LoCoMo and BEAM
histories are 21K and ~100K tokens, LongMemEval-S histories ~115K, so the improved
context is 6-30% of each history. Two cheaper levers were measured and rejected on
LoCoMo at 4,000 tokens: `bge-base-en-v1.5` instead of `bge-small` (0.9155 vs 0.9171,
three times slower to embed) and Personalized PageRank graph walk at weight 0.3
(0.9178). The adaptive budget delivers 0.931 at 4,567 tokens where a fixed 4,000
delivers 0.917 at 3,879.

### Answer accuracy: Gemma 4 31B as reader and judge

Paired on the same questions (stratified across categories and conversations, seed 0);
the interval is a paired bootstrap 95% CI of the difference.

**LoCoMo** (100 questions; Mem0/EverOS binary judge)

| Category | n | Baseline | Improved | Δ (95% CI) | Tokens |
|---|---:|---:|---:|---:|---:|
| single-hop | 26 | 76.9 | 96.2 | +19.2 (+7.7, +34.6) | 1447 → 6222 |
| multi-hop | 26 | 76.9 | 92.3 | +15.4 (+3.8, +30.8) | 1445 → 9028 |
| temporal | 25 | 76.0 | 84.0 | +8.0 (+0.0, +20.0) | 1451 → 5831 |
| open-domain | 23 | 39.1 | 73.9 | +34.8 (+13.0, +56.5) | 1445 → 6587 |
| **Overall** | 100 | 68.0 | **87.0** | +19.0 (+11.0, +27.0) | 1447 → 6937 |

On the baseline's full 200-question sample it scored 68.5%; 44 of its 63 misses were
"I don't have that information", 24 of them on open-domain questions that ask for an
inference ("Would Caroline pursue writing as a career?"). The remaining improved-arm
misses are mostly loose date arithmetic against the gold ("the weekend of 22 August"
for a "last weekend" said on 29 August) and commonsense inferences the reader still
declines.

### Served by a deployed gateway

[`benchmarks/deployed_recall.py`](../benchmarks/deployed_recall.py) runs a separate
`unimem gateway` process on a fresh store, writes all ten LoCoMo conversations through
`POST /v1/conversation/add` (vectors prepared at write time) and asks all 1,540
questions through `POST /v1/conversation/recall` with the improved settings, four
clients at a time, on a shared 16-core CPU host:

| Measure | Result |
|---|---|
| Served contexts byte-identical to in-process `recall` | 1,540 / 1,540 |
| Recall latency p50 / p95 | 152 ms / 283 ms |
| Recall throughput, 4 concurrent clients | 20.0 queries/s |
| Write latency p50 / p95 (≤200 messages, embedded at write) | 780 ms / 1,149 ms |
| Gold evidence delivered | 0.958 (single-hop 0.988, multi-hop 0.901, temporal 0.976, open-domain 0.796) |

## Caveats

- The same model is reader and judge. Published leaderboards mostly judge with GPT-4o
  or GPT-4.1-mini, so absolute numbers are comparable to them only approximately; the
  baseline and improved arms below share the judge, so their differences are exact.
- Gemma 4 on the Gemini Developer API is free of charge but limited to 16,000 input
  tokens per minute. Answer accuracy therefore uses stratified samples (every category
  equally represented, fixed seed); retrieval metrics use every question.
- LoCoMo is licensed CC BY-NC 4.0. None of the datasets is redistributed here.
