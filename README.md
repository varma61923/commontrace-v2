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
commontrace up --dest /path/to/agent-project
```

To verify local memory before connecting an agent, run `python scripts/first_recall_smoke.py`.
It creates a temporary store, adds one explicit fact, and checks that offline recall returns it;
the smoke needs no model key, server, or optional extra.

For local semantic retrieval, install `.[fastembed,vector-lance]` and place
FastEmbed's BGE-small weights in its local cache; the default profile is
cache-only and keeps lexical search available until weights are present.
Install `.[attention]` for other sentence-transformer models. Configure
models, budgets, routes and server settings in `commontrace.yaml`; optional
`settings.runtime.vector_backend: lance` enables background HNSW indexing.
`commontrace migrate config --dest STORE_ROOT --dry-run` previews migration
from legacy environment settings. Migration writes credential references,
and each request uses its own configuration context. See the
[configuration reference](docs/environment.md).

Open the printed console URL. The gateway requires its bearer token for memory
operations; Swagger UI is at `/v1/docs`, and its schema is at `/v1/openapi.json`.
For a scoped agent key, run `commontrace init --agent reviewer --dest STORE_ROOT`.
The key is returned once. See [installation and scope rules](docs/MEMORY_EVOLUTION.md).
In containers, `docker compose --profile local up` runs the gateway on `:8787` and
the MCP server on `:8421` over one store volume (`Dockerfile.local`).
For Docker daemons that ignore `Dockerfile.local.dockerignore`, build with
`python scripts/build_local_image.py --tag commontrace-local:dev`; it stages only
the local image inputs in a temporary context and removes that context after the build.

`up` initializes transactional storage, detects supported agents and installs
project-local hooks. Use `--setup-only` to configure without starting the API.
Add `--import-agent-history` to opt into a bounded import of Claude Code and
Codex transcripts whose recorded working directory belongs to the project.
A single `commontrace.yaml` selects the `local`, `team` or `server` profile:

```yaml
profile: local
profiles:
  local:
    embedding: fastembed:BAAI/bge-small-en-v1.5
    reranker: cross-encoder-fast
    redact_pii: true
    telemetry: false
```

Local model packages are optional; the default BGE-small path uses the
`fastembed` extra and cached weights, while `attention` provides other local
embedding and reranking models. Existing explicit environment settings take
precedence. Empty stores
wait for evaluated runs or reviewed history; setup never invents a first lesson.
Kimi setup merges hooks into the active user config at `~/.kimi-code/config.toml`
(or `$KIMI_CODE_HOME/config.toml`) and returns `kimi` as the activation command.
Cline requires Hooks enabled, and Windsurf's native hooks capture events; use
framework middleware for context.

## Add memory to your agent

```python
from commontrace import wrap_openai
completion = wrap_openai(provider_client, root=STORE_ROOT, agent_id="reviewer")
```

For a single save/retrieve operation with profiles and an unextracted buffer:

```python
from commontrace.client import MemoryClient
memory = MemoryClient("/path/to/agent-project", agent_id="reviewer", context={"user": "alice"})
turn = memory.turn("session-1", "How should I validate this patch?", text="I prefer pytest.")
# Feed turn["context"] as a user-level memory update; keep the system prefix stable.
```

Native framework callbacks are provided by `commontrace.framework_middleware.Middleware`;
see the [memory evolution guide](docs/MEMORY_EVOLUTION.md).

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

See the [marketplace and distribution guide](docs/distribution.md) for MCP clients, VS Code/Open VSX, JetBrains, GitHub Actions, PyPI and generated OpenAPI clients.

The [next-generation feature guide](docs/next-generation.md) covers configured providers, guided setup, conversation vector preparation, console history, generated SDK releases, and verified migration operations.

The [enterprise deployment and maintenance guide](docs/enterprise-readiness.md) covers verified database security,
renewable background-worker claims, crash recovery and measured long-session summary performance.

```bash
python scripts/export_openapi.py --check
python scripts/generate_sdks.py --languages typescript go rust java kotlin
```

The [OpenAPI contract](sdk/openapi.json) uses Swagger-compatible OpenAPI 3.0.
Every other HTTP route is described too: all gateway operations in
[`openapi/gateway.json`](openapi/gateway.json), tested against live responses, and the
Hub in [`hub/openapi.json`](hub/openapi.json) (see [openapi/](openapi/README.md)).
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
python -m benchmarks.adaptive_allocation_bench --reps 400
```

`commontrace allocate enable` switches a running experiment to adaptive allocation:
unproven memories are withheld at 50% until their effect is established, then
monitored at 5%, under published, hash-chained schedules and an estimator that stays
valid when rates change ([measurements](docs/benchmarks/adaptive-allocation.md)).

Benchmark manifests bind source, data, configuration and seeds. Published retrieval
metrics are evidence measurements; model-backed competitor accuracy and research
performance targets remain unverified. See the [scoreboard and reproduction
commands](docs/REFERENCE.md#measurement-scoreboard-2026-10-08), the dense,
reranked and adaptive-budget measurements in the [reference
documentation](docs/REFERENCE.md), and
[CausalMemBench](docs/benchmarks/causalmembench.md), which scores whether a memory
system stops delivering harmful memories without discarding helpful ones,
[PoisonBench](docs/benchmarks/poisonbench.md) and [GovBench](docs/benchmarks/govbench.md)
for memory poisoning and multi-principal governance. AMA-Bench and MemoryAgentBench
agent-memory results are in the implementation status.

The protocol is Capture → Structure → Extract → Validate → Store → Inject → Measure.
Read the [protocol](protocol/PROTOCOL.md), [full reference](docs/REFERENCE.md),
[architecture](docs/architecture.md), [changelog](CHANGELOG.md) and [AGPL-3.0-only license](LICENSE).
