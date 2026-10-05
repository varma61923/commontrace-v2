# Local execution of the repository's official benchmark infrastructure

These runs exercise actual benchmark datasets and the repository's evaluation code.
They do **not** establish superiority over Mem0 or the other competitors. The judged
run uses a small local Qwen model instead of the published GPT judge defaults, and
the BEAM runs measure retrieval rather than answer accuracy. No judge was edited.

## Scope and fixed configurations

Baseline: `4b555b0e3e88223b3556dab4430a2f75904f6a8f`.
Candidate: `8846feaccf94f6ad9dc37787c3c1f8fb63acdcca`, frozen in a detached checkout.
These revisions precede the subsequent ingestion and mapped-vector changes.
The baseline and candidate use identical selected inputs, seed 2026, one neighbor
on each side, four profile facts, and no cross-encoder reranker.

The machine provides a two-core CPU quota, an 8 GiB memory limit, and no GPU.
Generation and judging use the existing Qwen2.5-1.5B-Instruct Q4_K_M GGUF on CPU
through llama.cpp `d89651a7b205c03c4a0b13cd0646d400dc929f79`.
The server runs two inference/batch threads, one slot, an 8,192-token context,
seed 2026, and a 256-token output cap. The unchanged provider submits temperature 0.
Arctic embeddings load from the existing local cache with Hugging Face offline mode.
No paid provider or external model endpoint is used.

[run_official_local.py](run_official_local.py) creates a seeded subset in the source
dataset's original format, preserving the complete conversation history and gold
evidence. It invokes each checkout's unchanged `conversation_bench.main` and
observes HTTP responses to record model tokens, truncation and errors. Its only
runtime transport change is increasing the local CPU inference timeout to 300
seconds. Model weights, full datasets, stores and raw execution logs stay outside
the source tree. Input, subset, runner, judge and model checksums identify the run.
Existing results are never overwritten by the helper.

## Retrieval results on real BEAM histories

Each subset contains ten questions, one sampled per ability, from one complete
conversation. These BEAM runs are explicitly keyword-only (`--embedder none`),
not the candidate's new dense index. They make zero generation/judge calls.
Scores below are fractions of cited source message IDs reaching the final context.
Every corresponding baseline/candidate quality result is identical.

| Dataset | Full history, estimated tokens | Budget | Mean context tokens | Evidence recall | All cited evidence retained |
| --- | ---: | ---: | ---: | ---: | ---: |
| BEAM 100K | 130,636 | 1,500 | 1,472.3 | 84.81% | 77.78% |
| BEAM 1M | 1,060,628 | 1,500 | 1,469.1 | 31.25% | 12.50% |
| BEAM 1M | 1,060,628 | 7,000 | 6,906.9 | 43.93% | 25.00% |
| BEAM 10M | 12,326,295 | 1,500 | 1,468.8 | 39.16% | 22.22% |
| BEAM 10M | 12,326,295 | 7,000 | 6,889.3 | 64.16% | 55.56% |

The 10M conversation contains 19,895 messages across 6,228 sessions; both runs
completed. The 10M source uses nested plan/batch/turn objects; its pre-existing
normalized input was checked against the original: all 19,895 ordered message
dictionaries and the probing questions are identical, with a matching canonical
message hash recorded in the JSON. Its baseline/candidate ingestion times are 113.4/71.3 seconds,
with mean retrieval times 342.1/400.6 ms. Other agents' CPU profiles and local
generation were running concurrently, so these numbers are observations, **not**
an isolated performance comparison. The unchanged lexical path shows no quality
gain from the dense batching change, as expected.

The 1M/1,500-token subset misses cited evidence for contradiction resolution,
information extraction, preferences and summarization. Its temporal question
retains its cited source; its instruction, update and multi-session questions retain
half their cited sources. The 10M subset retains only 2.41% of the event-ordering
question's 83 cited sources, and none for its multi-session, preference or summary
questions. These are concrete recall gaps for future retrieval work.

Evidence recall averages per-question recall over questions with nonempty cited
evidence. All-evidence coverage averages whether every cited source was retained
over the same denominator. Abstention questions without citations are excluded
from those means. They cannot be treated as successful abstentions without answers.
The JSON includes gold evidence counts/IDs and per-question missing-source counts.
Retrieval precision is not reported: cited sources do not enumerate every useful
context passage. Lexical completeness and short-answer string presence are
diagnostics, not answer accuracy or calibrated confidence.

## LoCoMo answer/judge smoke run

Eight questions are sampled from one full LoCoMo conversation, two from each of
categories 1–4, with a 1,500-token context budget and Arctic embeddings. Adversarial
category 5 is excluded by the repository's standard parser. Both the answer and
judge use local Qwen. Both complete runs have a local judged score of **37.5%**,
**75%** evidence recall/all-evidence coverage and **1,451.875** mean estimated
context tokens. Per-category local judged scores are multi-hop 50%, temporal 0%,
open-domain 0%, and single-hop 100%; each category has only two questions.
All eight answer prompt hashes are identical between revisions, demonstrating
byte-identical retrieved contexts for these cases. There is no measured answer
quality improvement from batching in this subset.

Each run makes 16 model calls, with zero final HTTP errors, output truncations or
unparseable judge verdicts. Baseline/candidate model input totals are 18,938/18,918
tokens, and output totals are 458/438. Provider-reported cached prompt tokens are
2,603/17,883, explaining the faster candidate generation: server prompt reuse must
not be attributed to CommonTrace. The first retrieval includes cold local index
preparation; no steady-state retrieval latency claim is drawn from this run.

[Machine-readable results](../benchmark-results/official-local-results.json) include
category scores, original result checksums, model response observations, selected
question/source IDs and missing-source counts. Full dataset conversations and
stores are excluded from the committed artifacts.

The small judge is semantically unreliable despite returning parseable verdicts.
In the baseline, an answer giving the adoption year 2022 was graded WRONG against
the gold year 2022. A pet-name answer omitting one of the three required names was
graded CORRECT. Thus even a numeric score difference cannot establish an answer
quality gain. This run validates the generation/judge integration and exposes
failures for inspection; it is not a publishable competitive leaderboard result.

Initial detached server attempts were terminated by execution-session cleanup.
One earlier 60-second HTTP call retried after a canceled cold prompt prefill.
Those partial attempts are excluded. The final matched run keeps the server and
runner in one foreground PTY process group with the 300-second timeout. Server
prompt-cache state is observable in the model usage logs. Generation/judge latency
is reported separately from retrieval, with no cold/warm end-to-end speed claim.

## Final working-tree retrieval check

After the ingestion, mapped-vector and serving changes were integrated, the root
agent reran the same ten BEAM 10M questions at a 7,000-token budget using a copied
authoritative store snapshot. No new ingestion, embedding, generation or judging
was needed. Evidence recall remained **64.16%**, all-evidence coverage **55.56%**,
and mean context **6,889.3** estimated tokens. Category metrics, per-question
metrics and present/missing evidence-ID sets match the frozen `8846fea` run.
The final patches preserve this lexical retrieval contract; this check does not
measure the mapped dense index's answer quality.

The root's measured mean retrieval latency was 517.6 ms under shared CPU load,
so no speed gain is inferred. The JSON's `final_working_tree_validation` records
the source-file hashes identifying the final code under test, rather than assigning
uncommitted code the old HEAD's revision. The local Qwen service was stopped after
the matched LoCoMo run. No local judge was rerun for these final patches.

## Published reference values and protocol limits

[Mem0's official algorithm report](https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm)
(updated September 28, 2026) confirms the user-provided targets and token means.
It supplies the omitted BEAM 1M values: `temporal_reasoning` **61.8** and
`contradiction_resolution` **35.7**, plus LongMemEval `multi-session` **88.0**.
Its scores refer to Mem0's managed platform with proprietary optimizations;
they are not results from our pinned open-source Mem0 checkout. They also carry
judge variation. Our local model and sample sizes do not match that evaluation.

Actual repository category mapping is authoritative: LoCoMo category 1 is
`multi-hop`, 2 `temporal`, 3 `open-domain`, and 4 `single-hop`. The 82.3 target
belongs to `open-domain`, not a combined temporal category. The separate 97.0
target is LongMemEval `temporal-reasoning`. LongMemEval also uses
`single-session-user`, `single-session-assistant`, `single-session-preference`,
`knowledge-update`, `multi-session`, and dedicated abstention templates.
BEAM uses the ten snake_case abilities in `benchmarks/judges/beam.py`.

Repository LoCoMo scores are binary judge labels; LongMemEval scores use each
task's yes/no template. BEAM rubric scores average 0/0.5/1 compliance values;
repository event ordering uses normalized Kendall tau-b multiplied by F1.
The runner's BEAM `accuracy` thresholds a question's score at 0.5; `mean_score`
is the metric that preserves partial rubric credit.

Full upstream parity must not be presumed. The BEAM source comment references
commit `85048250af007704feccbb0bc46c6a8240b49a51`, whose raw source URL returned
404 during verification. Current official upstream
[`b2da22eac88bb0874c64665f13457eb99835774a`](https://github.com/mohammadtavakoli78/BEAM/tree/b2da22eac88bb0874c64665f13457eb99835774a)
uses semantic/LLM event alignment and additional per-rubric LLM grading in
`src/evaluation/compute_metrics.py`. The repository replica uses normalized
substring alignment without that extra grading. Its rubric parser also maps
arbitrary numeric scores into three bins. These are existing protocol differences,
not changes made for this evaluation. No BEAM judged accuracy is claimed here.
Dolphin's actual task/tool harness is distinct from the lightweight generic
conversation judge; it was not executed, and no Dolphin action score is claimed.

## Reproduce or evaluate the next frozen revision

Build and start a local model server; keep the server and evaluation process
group alive while the runner completes. Substitute local paths to the pinned
checkouts, dataset and model; the helper does not download or serve models.

```bash
cmake --build /path/to/llama.cpp/build --target llama-server -j 2
/path/to/llama.cpp/build/bin/llama-server \
  --model /path/to/qwen2.5-1.5b-instruct-q4_k_m.gguf \
  --host 127.0.0.1 --port 18081 --ctx-size 8192 \
  --threads 2 --threads-batch 2 --parallel 1 --n-predict 256 --seed 2026

python research/run_official_local.py --dataset locomo --data /path/to/locomo10.json \
  --checkout /path/to/baseline --checkout /path/to/candidate \
  --output /path/to/new-empty-locomo-results --per-category 2 \
  --model-file /path/to/qwen2.5-1.5b-instruct-q4_k_m.gguf

python research/run_official_local.py --dataset beam --data /path/to/beam-1M.parquet \
  --checkout /path/to/baseline --checkout /path/to/candidate \
  --output /path/to/new-empty-beam-results --retrieval-only --embedder none --budget 7000
```

For full official runs, use `benchmarks/conversation_bench.py` directly with
`--dataset locomo|longmemeval|beam`, the full source file, `--answer`, and an explicit
answer/judge model on a local OpenAI-compatible endpoint. LongMemEval `--limit`
samples by base task type; LoCoMo's limit stops after a conversation, and BEAM's
limit counts conversations rather than questions. The helper avoids confusing
these semantics when making a small balanced subset. Full LongMemEval and full
multi-conversation BEAM judged runs remain outstanding, as do matched competitor
executions. The benchmark directory is required by CLI/tests and is retained.
