# Memory evolution

Markdown is the source of truth for traces, lessons and controls. Facts and graph
journals remain canonical JSONL; indexes are rebuildable. ADD-only extraction
never overwrites facts, and valid time remains separate from recorded time.

## Start

```python
from commontrace import wrap_openai
completion = wrap_openai(client, root=STORE_ROOT, agent_id=AGENT_ID)
```

`wrap_anthropic` and `wrap_litellm` are also available without mandatory provider
SDK dependencies. Call the wrapped completion with an `occasion_id`; report an
independently evaluated outcome with `MemoryClient.outcome(id, succeeded=True)`.
Completions and git commits never imply success. Streaming wrappers reject
requests before delivery because their capture contract requires a full answer.

```python
from commontrace.client import MemoryClient
memory = MemoryClient(STORE_ROOT, agent_id=AGENT_ID)
memory.add("The release window is tomorrow", memory_type="temporary")
memory.search("release", recipe="diverse")
memory.profile("release")
memory.reflect("release", budget=600, occasion_id="task-42", exploration_slots=1)
memory.outcome("task-42", succeeded=True)
```

For HTTP use `MemoryClient(url=GATEWAY_URL, token=AGENT_TOKEN)`. The local binary
and Python gateway share these API handlers. HTTP agent scopes come from the
registered principal, replacing payload-supplied labels. Orthogonal `user`,
`agent`, `app`, `project` and `session` scopes must all match.

## Install and operate

```bash
commontrace evolve install --agent-id reviewer --repo /path/to/code --commits 100 --dest /path/to/store
commontrace gateway --allow-self-signup --dest /path/to/store --port 8787
commontrace evolve offline --max-jobs 20 --seconds 30 --dest /path/to/store
commontrace dream --max-model-calls 10 --max-tokens 50000 --max-cost-usd 1 --dest /path/to/store
```

Installation imports bounded git metadata as raw experience and writes an SDK
`SKILL.md` and per-agent plugin with executable JSON hooks under `distribution/`.
Hook inputs require an `occasion_id`; start takes `query`, end takes `context`
and `solution`, with an optional explicitly boolean `succeeded`. Credentials are
returned once and never stored in the plugin. Reinstallation requires `--rotate`,
revoking the old key. These portable hooks require the assistant runtime to map
its session payload to this format; native assistant integrations are not verified.

Owner routes include `/v1/agent/signup`, `/v1/agent/rotate`, `/v1/agent/claim` and
`/v1/agent/plugin`. Opt-in `/v1/agent/enroll` assigns a fresh isolated identity,
with enrollment and unclaimed-agent operation quotas. Agents may only use their
own plugin, heartbeat and scoped memory API. Heartbeats enqueue due standing
questions. Local filesystem authority is the operator trust boundary.

## Retrieval and offline memory

| Recipe | Ranking |
| --- | --- |
| balanced | Dense cosine + sigmoid-normalised BM25 + entity + temporal signals |
| diverse | MMR |
| nearby | Graph distance from a required center |
| corroborated | Distinct source-episode mentions |
| recent | Stronger temporal weighting |
| decision | Supported exploration-derived causal utility prior |

`commontrace evolve recipes` lists recipes. The retriever registry supports
hybrid, decomposition, bounded graph completion and local category queries.
Custom graph-neighbor backends are injectable; canonical writes use JSONL.
Feedback adjusts graph-edge weights. Filters for evidence, scope, forgetting,
validity and origin run before scoring. Set `COMMONTRACE_FACT_EMBEDDER_PATH` to
an existing local embedding model directory to enable dense ranking; absent
that optional dependency, retrieval uses the available signals. GLiNER accepts
local weights or an injected adapter and links only literal admitted text.

`profile` returns static and dynamic facts together. `reflect` packs curated →
consolidated → raw evidence and mandatory directives using a conservative
UTF-8-byte/4 token estimate. No guaranteed 50 ms latency or model-specific token
cap is claimed. Directives cannot be held out; call `memory.check_action` at the
external tool boundary to enforce deny-tool and required-tag rules.

Standing questions refresh through the durable background queue. Cached answers
are reusable only while their evidence is live and unchanged. Offline passes
also consolidate proof-counted observations, draft reviewed anticipatory notes
from request history, and cluster instrumented success/failure cases into
`SKILL.md` proposals. The Memory Palace console exposes review actions.

Session distillation seals its watermark only after a valid model response and
all proposals persist. Failed calls remain retryable. `LLMRuntime` routes calls
by purpose and tracks configured prices and reported usage; missing accounting
or failed calls stop subsequent dispatch. In-flight work can exceed a budget.
Core ingestion, retrieval and offline refresh operate without an LLM.

Prescribed JSON Schema/Pydantic ontologies validate graph properties; learned
schemas remain proposals. Install `commontrace[ontology]` for full JSON Schema
validation, `commontrace[extraction]` for GLiNER, and `commontrace[security]` for
Ed25519 primitives. Batch ingestion enforces 200 items, bounded statements,
explicit-timezone timestamps and aliases. Temporary facts default to 24-hour
expiry; environment facts to 30 days. Typed decay never deletes history.

## Causal decisions and governance

Reserved exploration slots log sampling propensities and assignment before
context delivery; they cannot redeliver held-out evidence. Delayed outcomes
support self-normalised IPW, decision priors, reward export and deletion/reanswer
probes with plan/execution attribution. These estimates are experimental; the
existing preregistered holdout remains billing authority.

Skill proposals retain structural signatures, applicability limits, evidence
and Beta reliability. Independent review and an abstraction-versus-raw causal
gate precede publication. Admitted skills recheck current evidence, scope and
origin; harmful or unmeasurable verdicts demote them. Operator-supplied experiment
bounds are not independently verified billing proofs or a trained PPO policy.

New evolution facts, traces and controls receive write-time origin receipts.
Derived authority cannot exceed its least-trusted source. Signed bytes are
checked during recall, and deleting a receipt cannot convert a bound record to
unsigned legacy data. Optional `memory/authority-policy.yaml` sets minimum
origin authority per action. Legacy writers and records remain compatible;
this is not a theorem against compromised operators or adaptive collusion.

MemFS signs committed snapshots and audience-bound shared-repository handoffs.
Many agents may attach the same read-only store; expiry and drift are rechecked.
Local HMAC keys must remain private. Public-key origin primitives support
configured Ed25519 signers; MemFS handoffs still use local HMAC custody.

Protected federation exports approved public procedure shape and aggregate
reliability, excluding raw traces, scopes and free-text applicability. The
optional private exporter uses experimental Laplace-noised counts and a durable
noise budget. Eligibility and finite-precision sampling are not a verified
differential-privacy mechanism. Imports
require configured signer approval over exact payload bytes and remain review
proposals. No external transmission or federated trainer runs automatically.

## Build and reproduce

```bash
bash scripts/build-local-binary.sh
./dist/commontrace-local gateway --dest /path/to/store --port 8787
./reproduce.sh
python3 -m benchmarks.evolution --adapter vendor=module:factory
```

The core native binary bundles Python, YAML, schemas and console assets. Heavy
local extraction/embedding models require the optional Python installation; build for
each target OS/architecture. The open benchmark adapter requires mapped evidence
IDs, context measurement and reported or unknown cost. Manifests snapshot code,
config, data hashes and seeds. Two independent synthetic runs compare functional
metrics, with latency reported separately; the gateway serves their benchmark
page. Real-data vendor adapters and strict-budget confirmation runners are also
available. Existing recorded runs predate these latest integrations and do not
establish current accuracy gains. Public hosting, trained memory-manager policies,
replica-wide verified erasure and research performance/security targets require
separate evaluation and infrastructure.
