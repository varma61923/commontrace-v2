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

## Caveats

- The same model is reader and judge. Published leaderboards mostly judge with GPT-4o
  or GPT-4.1-mini, so absolute numbers are comparable to them only approximately; the
  baseline and improved arms below share the judge, so their differences are exact.
- Gemma 4 on the Gemini Developer API is free of charge but limited to 16,000 input
  tokens per minute. Answer accuracy therefore uses stratified samples (every category
  equally represented, fixed seed); retrieval metrics use every question.
- LoCoMo is licensed CC BY-NC 4.0. None of the datasets is redistributed here.
