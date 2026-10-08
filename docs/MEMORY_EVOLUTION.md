# CommonTrace memory evolution

CommonTrace already shares EverOS's central storage philosophy: **Markdown is
the source of truth for traces, lessons and now memory controls; indexes are
rebuildable.** Existing facts and graph journals remain canonical JSONL. This
branch preserves that distinction instead of claiming that every store is Markdown.
Facts are append-only, with separate valid and recorded times. Extraction never
chooses an UPDATE, DELETE, approval or supersession operation.

## Start with two lines

```python
from commontrace.client import wrap
completion = wrap(your_completion_callable, root=STORE_ROOT, agent_id=AGENT_ID)
```

Call `completion(messages=[...], occasion_id="task-42", ...)` using an
OpenAI-style completion callable. The wrapper recalls curated → consolidated →
raw evidence, respects the store's holdout and harm policy, and captures the
completed exchange as a raw trace. A generated answer is not labelled success.
After independently evaluating the task, report the result:

```python
from commontrace.client import MemoryClient
memory = MemoryClient(STORE_ROOT, agent_id=AGENT_ID)
memory.outcome("task-42", succeeded=True)
```

Use your own occasion IDs to connect retrieval to eventual feedback. Directives
never enter a holdout. Call `memory.check_action(tool, tags=[...])` before a tool
acts: the SDK can enforce deny-tool and required-tag rules at this boundary.
Natural-language directives also appear in context, but cannot by themselves
enforce an external tool runner that does not use the gate.

## Coding-agent installation and distribution

```bash
commontrace evolve install --agent-id reviewer --repo /path/to/repo --commits 100 --dest /path/to/store
```

The installer imports bounded git metadata as raw experience, writes an SDK
`SKILL.md` and per-agent plugin manifest under the destination's `distribution/`,
and returns an agent token once. Commit presence is never treated as a successful
task outcome. The root code-review `SKILL.md` remains its own reference profile.
Store the returned token in your runtime secret configuration; it is not written
into the plugin. An existing agent cannot request another agent's token.

For HTTP, start the existing gateway and register using its owner credential:

```text
POST /v1/agent/signup       {"agent_id":"reviewer"}
POST /v1/agent/plugin       {"agent_id":"reviewer"}
POST /v1/agent/heartbeat    {"agent_id":"reviewer"}
```

The owner credential is required to bootstrap signup. An issued agent token
grants only `/v1/memory/*`, its own plugin and its own heartbeat. Its registered
scopes replace caller-supplied scope labels. It cannot create directives, approve
lessons, inspect the entire console, or register Sybil identities.
The new evolution routes require a dedicated store and reject container-tagged
requests. Existing container-scoped recall/review routes retain their namespace
contract; body context cannot switch an evolution request into another container.

```python
memory = MemoryClient(url=GATEWAY_URL, token=AGENT_TOKEN)
memory.add("Explicitly supported fact", local=True)
memory.profile("release")
memory.reflect("release", budget=600)
```

Local and HTTP operations use the same admission/retrieval functions. HTTP agent
writes use explicit assertion mode; remote agents cannot spend the owner's LLM
budget. Owner/local callers can request a single extraction pass with
`commontrace evolve add "..." --model`. Contradictory facts coexist until an
explicit governed supersession. Exact replays never change confidence, scope,
stability, validity or evidence counters.

## Retrieval and context

```bash
commontrace evolve recipes
commontrace evolve search "release approval" --recipe diverse --dest /path/to/store
commontrace evolve search "release" --recipe nearby --center project:meridian --dest /path/to/store
commontrace evolve search "release and deployment" --retriever decomposition --dest /path/to/store
```

| Recipe | Behavior |
|---|---|
| balanced | Dense + query-calibrated sigmoid BM25 + entity + temporal signals |
| diverse | Greedy MMR with a lexical redundancy penalty |
| nearby | Graph node-distance ordering with a required center |
| corroborated | Distinct source-episode mentions before score |
| recent | More temporal weight and a shorter half-life |
| decision | Complete exploration-derived causal utility as a ranking prior |

Dense cosine scores come from an optional adapter; no embedding service means
lexical/entity/temporal retrieval, without fabricated dense scores. All recipes
filter current proof, validity, forgetting, expiry and orthogonal scopes before
computing lexical statistics. The registry supports hybrid, decomposition,
bounded graph completion and a local `category:<name>` NL-to-filter adapter.
Custom retrievers and `GraphBackend.neighbors` implementations can be registered
or injected. This branch does not replace the canonical graph write backend.
Graph completion returns evidence and does not expose hidden chain-of-thought.

`profile` reads canonical facts once for static and dynamic sections, together
with hard directives. Profile delivery also participates in holdout measurement.
~50 ms is a target, not a guaranteed latency. `reflect` shares a conservative
UTF-8-byte/4 context budget across abstraction levels; it refuses a budget too
small for mandatory directives. This estimate is not a model-specific tokenizer.
It deduplicates selected evidence and excludes an abstraction's selected raw
sources to reduce holdout leakage. Existing conversation recall retains its
separate token-budgeted history engine.

## Standing questions, rules, proposals and foresight

```bash
commontrace evolve directive "Release requires review" --deny-tool deploy --dest /path/to/store
commontrace evolve question "What release risks need attention?" --refresh-seconds 3600 --dest /path/to/store
commontrace evolve offline --max-jobs 20 --seconds 30 --dest /path/to/store
commontrace evolve foresight "Check tomorrow's release window" --source trace-id \
  --valid-from 2026-10-09T00:00:00Z --expires-at 2026-10-10T00:00:00Z --dest /path/to/store
```

Controls are immutable Markdown revisions under `memory/controls/`. Mental
models are standing questions; the durable jobs queue refreshes dated answers
from current evidence without a model call. Their stored answers are snapshots
for inspection, not automatically re-injected truth. Offline/cron passes enqueue
due refreshes. Foresight notes carry explicit validity, expiry, evidence links
and review status; they are anticipatory proposals, not established facts.

`distill_session` advances a successful watermark only after its model response
validates and every proposal persists. A failed call, malformed output, or failed
write leaves those entries unsealed. Retry IDs are deterministic. Proposals
remain review-only; rejection checks the expected revision.

The console's **Memory Palace** view shows hard rules, standing questions,
pending suggestions and exhausted jobs, with refresh, evidence inspection and
rejection actions. HTTP rule creation and rejection additionally require the
gateway's existing `--allow-approval` switch. CLI changes use the local operator's
filesystem authority.

`user`, `agent`, `app`, `project` and `session` labels are orthogonal: every
declared dimension must match. These routing labels do not replace HTTP
authentication. Legacy unprefixed scopes retain their existing semantics.

## Offline compute, schemas and local extraction

```bash
commontrace dream --no-draft --max-seconds 120 --dest /path/to/store
commontrace dream --max-model-calls 10 --max-tokens 50000 --max-cost-usd 1 --dest /path/to/store
commontrace dream --recipe cron --every daily --dest /path/to/store
commontrace ontology schema ontology.schema.json --dest /path/to/store
commontrace ontology propose --dest /path/to/store
```

`LLMRuntime` routes explicit purposes to owner-supplied provider configurations
and accounts for calls, reported tokens, configured prices and failures.
Context-local routing avoids globally monkeypatching an LLM provider. Dreaming
uses it for distillation/consolidation and records `memory/dream_runs.jsonl`.
Budgets prevent starting subsequent work; an in-flight call can finish after
its time/token/cost cap. An unpriced call stops further monetary-budgeted calls
and is labelled unpriced, never recorded as known spend.

The offline pass consolidates observations, refreshes mental models and clusters
explicitly instrumented procedure cases into review-only `SKILL.md` proposals.
Supply `extensions.profile.procedure.steps` and a boolean `outcome.resolved` on
raw traces to make those cases eligible. Skill proposals retain source traces,
structural signatures, applicability bounds and Beta reliability parameters.
No silent inferred outcomes or automatic skill approval.

`Ontology.from_models` accepts Pydantic v1/v2 JSON-schema methods without making
Pydantic a dependency. Prescribed schemas and learned graph-schema proposals
are separate operations; learned ontologies never activate themselves. GLiNER
is an optional injected/local-directory extraction adapter. It requests local
weights only, and was tested with an injected model, not downloaded weights.
Core ingestion, retrieval, graph operations and offline refresh work without an
LLM. Local provider inference remains optional.

`ingestion_contract.batch` and `POST /v1/memory/batch` share atomic ADD-only
validation, a 200-item cap, canonical ontology aliases, bounded text and
timezone-explicit timestamps. Per-type decay extends `decay.py`; existing TTL
read gates automatically hide expired facts without overwriting historical data.
Use `memory.add("Temporary assertion", memory_type="temporary")` or the CLI's
`--memory-type temporary` for a write-time 24-hour default expiry; environment
assertions default to 30 days. Explicit expiry takes precedence.

## Signed repositories and causal research surfaces

```bash
commontrace memory sign --issuer owner --dest /path/to/shared-store
commontrace memory attach /path/to/shared-store --agent-id reviewer --handoff TOKEN --dest /path/to/store
```

Detached HMAC snapshot receipts extend existing signed MemFS handoffs. Many
local agents can attach the same read-only shared repository. Reads verify the
audience, expiry, full-memory scope and both committed/uncommitted drift. Shared
attachment is a local SDK feature in this branch; HTTP does not expose arbitrary
filesystem attachment. HMAC signing relies on trusted key custody and is not a
public-key multi-organization trust system.

`causal_policy` supplies reserved-slot sampling with known propensities,
append-only delayed outcomes, self-normalised IPW, reward export, paired
deletion/reanswer probes and plan/execution attribution. Estimates require both
randomised arms and complete outcomes; decision priors need effective sample
support. These experimental estimates are not billing proof. The existing
preregistered holdout/proof flow remains the billing authority.

`origin` binds configured principal, organization, authority and record bytes;
corroboration counts independent configured organizations on the same digest.
It also supplies descriptive co-voting flags. These are primitives, not a proof
of resistance to compromised signers, laundering or adaptive collusion, and are
not automatically applied to every legacy writer.

`federation.prepare/export` exports only aggregate reliability and structural
steps mapped into operator-approved public tool categories. It excludes raw
traces, tenant identity, scopes, input values and free-text applicability. Export
requires a configured principal's `federation-export` approval bound to the exact
public payload digest. This is a protected exchange format, not differential
privacy, an external transmission, or a federated-learning trainer.

Compression-level changes and abstraction-vs-raw gates are evidence-linked
policy primitives. PPO training, learned memory-manager deployment, automatic
promotion/demotion, randomized high-stakes tool voting, verified physical erasure
across replicas, and cross-organization federated training remain follow-up work.
No security theorem or paper's compression/transfer speedup is claimed here.

## Native binary and reproducible benchmark site

```bash
python3 -m pip install pyinstaller           # build-time dependency only
bash scripts/build-local-binary.sh
./dist/commontrace-local gateway --dest /path/to/store --port 8787
./reproduce.sh
commontrace gateway --dest "$PWD" --port 8787
```

The Linux native binary bundles Python, the package, YAML, schemas and console
assets, serving the same API as the Python/cloud gateway. Build on each target
OS/architecture; the current artifact is Linux x86-64.

`/benchmarks/` serves the generated synthetic showcase. `reproduce.sh` runs two
independent processes and compares functional metrics while reporting latency
separately. `benchmarks.evolution.Adapter` is an open vendor adapter interface:
`python3 -m benchmarks.evolution --adapter vendor=module:factory`. Adapters must
map evidence IDs, measure returned context and report cost or unknown cost.
Each run snapshots source revision, dataset hash, seed, recipes and environment.
The fixture measures recall, a deterministic next-action evaluator, context,
latency, cost, ablation credit and randomized fixture utility. No paid vendor
evaluation or external internet publication was performed. Real agentic
DolphinBench/MemoryArena/etc. runs require their datasets, harnesses and model
credentials; the existing DolphinBench harness remains available.

## Reference and validation records

- [Pinned upstream checkouts](research/upstream-manifest.json)
- [2025–2026 research digest](RESEARCH_DIGEST.md)
- [Resolved arXiv metadata](research/arxiv-metadata.json)
- Integrated behavioral tests: `tests/test_memory_evolution.py`

Only CommonTrace is modified. Upstream repositories were read as design
references; their implementations and license obligations were not silently
vendored into this package.
