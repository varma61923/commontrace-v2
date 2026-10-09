# CommonTrace

**Memory that proves it works.**

CommonTrace gives agents persistent memory and measures whether that memory
improves their outcomes. Its randomized holdout can identify harmful memories,
withdraw them, and produce signed evidence for review and value-based billing.
Bring an existing memory provider or use the local store.

Markdown is the source of truth for traces, lessons and controls; indexes are
rebuildable. Facts and graph history use append-only journals. The core runs
without an LLM and requires only Python 3.10+ and PyYAML.

## Start locally

```bash
git clone https://github.com/varma61923/commontrace-v2.git
cd commontrace-v2
python -m pip install -e .
commontrace up --dest ../commontrace-store
```

Open the printed console URL. The gateway requires its bearer token for memory
operations; Swagger UI is at `/v1/docs`, and its schema is at `/v1/openapi.json`.
For a scoped agent key, run `commontrace init --agent reviewer --dest STORE_ROOT`.
The key is returned once. See [installation and scope rules](docs/MEMORY_EVOLUTION.md).

## Add memory to your agent

```python
from commontrace import wrap_openai
completion = wrap_openai(provider_client, root=STORE_ROOT, agent_id="reviewer")
```

Call `completion` with `messages` and an `occasion_id`. Recall happens before
completion; the response is captured as experience. Report an independently
observed outcome through `MemoryClient.outcome`. A completion never implies
success, and generated proposals require review before activation.

You get ADD-only fact extraction, named hybrid search recipes, static and dynamic
profiles, standing questions, hard directives, budgeted offline consolidation,
shared signed memory repositories, and an evidence-governed compression ladder.
Origin receipts preserve the least-trusted source through summarization and echo.

Lessons that proved their lift at two or more organizations can be listed and
bought as signed listings (`commontrace market`); an install always lands in review.
`COMMONTRACE_LLM_PROVIDER=local` runs drafting with an in-process model, no key needed.

The [memory guide](docs/MEMORY_EVOLUTION.md) covers local models, connectors,
framework tools, causal policy evaluation, replay forensics and their limits.
The multi-tenant [Hub](hub/README.md) adds organization isolation and governance.

## Standard API and generated SDKs

```bash
python scripts/export_openapi.py --check
python scripts/generate_sdks.py --languages typescript go rust java kotlin
```

The [OpenAPI contract](sdk/openapi.json) uses Swagger-compatible OpenAPI 3.0.
A checksum-pinned OpenAPI Generator produces the clients into `sdk/generated/`.
CI validates the contract, compiles all five clients and publishes build artifacts.
See [SDK instructions](sdk/README.md). Tagging `vX.Y.Z` runs the release workflow,
which publishes to PyPI and npm through trusted publishing once those registries are
configured for this repository; hosted service deployment is a separate step.

## Verify

```bash
python -m pytest tests/ e2e_tests/ -q
bash reproduce.sh
python -m benchmarks.causalmembench --seeds 10
```

Benchmark manifests bind source, data, configuration and seeds. Published retrieval
metrics are evidence measurements; model-backed competitor accuracy and research
performance targets remain unverified. See the [scoreboard and reproduction
commands](docs/REFERENCE.md#measurement-scoreboard-2026-10-08), the dense,
reranked and adaptive-budget measurements in the [implementation
status](docs/strategy/implementation-status.md#measured-results), and
[CausalMemBench](docs/benchmarks/causalmembench.md), which scores whether a memory
system stops delivering harmful memories without discarding helpful ones,
[PoisonBench](docs/benchmarks/poisonbench.md) and [GovBench](docs/benchmarks/govbench.md)
for memory poisoning and multi-principal governance. AMA-Bench and MemoryAgentBench
agent-memory results are in the implementation status.

The protocol is Capture → Structure → Extract → Validate → Store → Inject → Measure.
Read the [protocol](protocol/PROTOCOL.md), [full reference](docs/REFERENCE.md),
[architecture](docs/architecture.md), [changelog](CHANGELOG.md) and [MIT license](LICENSE).
