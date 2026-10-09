# Scorecard: targets A-G

Each target from the master prompt, its status on this branch, the measured number and the command that
produced it. "Met" needs a measurement; a feature that exists but has not been measured is "not measured".
Details and the full audit are in [implementation-status.md](implementation-status.md).

| Target | Status | Measured | Command |
| --- | --- | --- | --- |
| **A.** Accuracy parity with re-run competitors, LLM-judged, with CIs | Not met | Evidence share on full LoCoMo at 1,500 tokens: 78.69% lexical, 82.00% dense, 83.90% dense + cross-encoder. A 0.5B local reader with an exact judge: 4% no memory, 19% lexical, 26% dense adaptive (100 questions). No frontier reader and judge measured yet | `python -m benchmarks.conversation_bench --dataset locomo ...` |
| **B.** Top-3 on two agentic leaderboards, with cost and latency | Not established | AMA-Bench evidence 66.90% / 78.62% (fixed), 72.87% / 83.93% (adaptive); MemoryAgentBench answer-in-context 64.82% / 71.11% and 67.84% / 76.63% at 1.5K / 4K. Not the benchmarks' official model-judged scores | `--dataset ama`, `--dataset mab` |
| **C.** >= 90% evidence within 1,000 tokens on LongMemEval-S | Not met | At 1,000 tokens (100-question subset): lexical 74.66%, dense 77.89%, dense + cross-encoder 84.18%, 86.56% with the cross-encoder weighted 3x (not confirmed on held-out LoCoMo, so not the default). The remaining gap is ranking gold turns inside correctly found sessions | `--dataset longmemeval --budget 1000 --embedder arctic-m --rerank cross-encoder --limit 100` |
| **D.** Hybrid recall p50 < 50 ms / p95 < 150 ms at 1M memories, recall@10 >= 0.97 of exact | Met for the vector path; hybrid not measured | 1M synthetic 384-d vectors, pgvector HNSW (m 24, ef_construction 200), ef_search 100: p50 7.2 ms, p95 10.7 ms, recall@10 1.000. Default build (m 16): recall plateaus at 0.945 | `python -m benchmarks.vector_scale_bench --n 1000000` |
| **E.** +5pp at 80% power within 2,000 occasions, anytime-valid coverage >= 95% | Coverage met; power not met | Time-uniform coverage >= 96.7% in all 45 cells; +5pp power 0.31-0.33 (no predictive stratum) to 0.64-0.70 (strong); +10pp 0.96-1.00 with adaptive allocation | `python -m benchmarks.adaptive_allocation_bench --reps 400` |
| **F.** 0% attack success on poisoning suites at >= 95% clean utility | Met (on PoisonBench) | 0% across nine attacks with an authority policy, 100% clean utility; without one, all six origin attacks succeed | `python -m benchmarks.poisonbench --variants 5` |
| **G.** `pip install` / `npm i` / `docker run` to first recall in < 60 s | Not measured | Release workflow, generated SDKs and local image exist; nothing published to registries | — |

## Beyond the targets

| Area | Measured | Command |
| --- | --- | --- |
| Confounded withdrawal | Correlational withdrawal lost a helpful memory in 10/10 runs; randomized withdrawal never withdrew a memory that does not hurt | `python -m benchmarks.causalmembench --seeds 10 ...` |
| Multi-principal governance | 100% utility, 0 leaks in 117 checks, 0 forgotten records delivered in 36 | `python -m benchmarks.govbench` |
| Embodied fleet | Real-floor shortcut HURTS -0.186 [-0.328, -0.037] (truth -0.200); sim/real pooling refused; withdrawn 100/100 | `python -m benchmarks.robot_fleet_demo --occasions 1200` |
| API contract | 58 gateway operations validated against live responses; 106 Hub operations in OpenAPI 3.1 | `python scripts/export_openapi.py --check`, `python scripts/export_hub_openapi.py --check` |
