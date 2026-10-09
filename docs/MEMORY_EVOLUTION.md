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
commontrace up --dest /path/to/store
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
`commontrace[graph]` provides source-bound Neo4j/FalkorDB snapshots with staged
publication and stale-writer rejection. `commontrace[vector-lance]` supplies a
scoped LanceDB vector index for the existing async seam. The exact SQLite and
pgvector engines remain available. `commontrace.store.FilesStore` and
`SQLiteStore` add a canonical Markdown record interface; they do not automatically
migrate legacy SDK fact stores.
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

Conversation recall can size its own context. `commontrace conversation recall
SPACE "QUESTION" --budget auto` (MCP and gateway: `adaptive_budget`) keeps a
focused question at its base budget and gives summaries, orderings and counts up
to 3x and lists or multi-facet questions 2x, capped at `--max-budget` (default
12,000); `explain.budget` records the decision. Second-stage rerankers resolve by
name: `retrieval.apply_reranker(task, ranked, providers.reranker("mmr"))`, with
`cross-encoder` and `cross-encoder-fast` built in and `register_reranker` for your own.

## Causal decisions and governance

Reserved exploration slots log sampling propensities and assignment before
context delivery; they cannot redeliver held-out evidence. Delayed outcomes
support self-normalised IPW, decision priors, reward export and deletion/reanswer
probes with plan/execution attribution. These estimates are experimental; the
existing preregistered holdout remains billing authority.

`commontrace policy register CANDIDATE.json` freezes an exploration-delivery policy
before fresh evaluation traffic. `policy evaluate` reports occasion-level IPS,
SNIPS, support/ESS and confidence bounds; DR requires a frozen complete joint-action
prediction table trained only on earlier occasions. `policy install` and
`gate --policy CANDIDATE.json` fail closed without a supported non-harm verdict.
These are preregistered fixed-horizon comparisons with multiplicity correction;
arbitrary ranker, embedding or prompt changes are refused because these logs do
not identify them. The default billing holdout remains anytime-valid.

The `compression` commands propose every adjacent level from trace through
directive, register randomized candidate/parent/raw comparisons, record outcomes
and require independent revision-checked review. Active artifacts recheck signed
admission, source hashes and later harm verdicts. `export-training` exports
source-linked SFT examples; no training job runs automatically.

`assurance forensics` replays deletion probes through a trusted local evaluator
and signs the incident report. `assurance action-vote` checks current evidence,
directives and origin policy, then requires empty/full/randomized context votes
to agree. Its elapsed budget is checked after callbacks; it cannot terminate a
blocked callback. Recheck policies at execution time. Provider-reported usage and
plan/execution/environment attribution are signed beside the recall receipt;
replay sensitivity and usage accounting are not billing proofs.

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
configured Ed25519 signers. `assurance signing --algorithm ed25519` pins a per-store
public key and enables publicly verifiable record and MemFS snapshot signatures;
verifiers cannot sign. Legacy HMAC receipts remain verifiable and shared MemFS
handoffs still use local HMAC custody. Recursive forgetting blocks descendants at
canonical admission; `assurance forget-certificate` describes local retrieval
surfaces while explicitly excluding historical-byte and remote-replica erasure.

Protected federation exports approved public procedure shape and aggregate
reliability, excluding raw traces, scopes and free-text applicability. The
optional private exporter uses experimental Laplace-noised counts and a durable
noise budget. Eligibility and finite-precision sampling are not a verified
differential-privacy mechanism. Imports
require configured signer approval over exact payload bytes and remain review
proposals. No external transmission or federated trainer runs automatically.

`federation.randomized_response` additionally implements exact binary randomized
response with epsilon=log(3), delta=0 and a composed durable accountant. Its event
outcome replacement guarantee assumes public, fixed cohort membership and public
procedure structure; it does not protect those public fields or participant
membership. `replicated_lift` authenticates distinct configured organizations,
enforces artifact/comparison/metric and sim/real agreement, and reports descriptive
random-effects pooling with I-squared. It is not a verified billing proof.


`commontrace experiment --by agent_type` (or `agent_id`, or `--covariates FILE`
with your own pre-treatment groups) reads each lesson's randomized effect per
subgroup, Benjamini-Hochberg corrected, with Cochran's Q for heterogeneity. A
lesson that helps one subgroup and hurts another is flagged `CROSSING`; narrow its
`applies_when`. These readings are exploratory and fixed-horizon, never the billing
verdict. `CausalMemory(..., graduate=True)` stops randomizing a memory once its
anytime-valid verdict is HELPS, so proven memories stop paying for their holdout.
`python -m benchmarks.causalmembench` scores this layer against seeded ground truth
([results](benchmarks/causalmembench.md)).

## Standard API and integrations

Swagger UI is at `/v1/docs`; `/v1/openapi.json` describes live routes. The public
memory SDK [OpenAPI contract](../sdk/openapi.json) has typed inputs and outputs and
shares its validation definitions with gateway admission. Run
`python scripts/generate_sdks.py` to generate TypeScript/Go/Rust/Java/Kotlin clients
with the checksum-pinned upstream OpenAPI Generator. CI compiles each generated
client and publishes artifacts; no custom SDK templates are used.

Framework factories include native Google ADK, Strands and AG2 1.x tools and the
older AG2 registration API. The TypeScript convenience SDK accepts real Vercel AI
and Mastra factories. CI runs these SDKs without model calls. Assistant installer
targets include Codex, Copilot, Gemini and OpenCode; their generated MCP fragments
require an operator merge and do not establish verified native session hooks.

`commontrace connect PROVIDER RESOURCE --token-env TOKEN_VARIABLE --context agent:ID`
imports read-only provider knowledge with pinned origins, source snapshots and
failure-safe cursors. Providers include GitHub, Slack, Jira, Linear, ServiceNow
incidents, Salesforce Cases, Notion, Drive text exports, Gmail, Confluence pages,
OneDrive, Zendesk articles, Greenhouse candidates and Intercom conversations.
These are bounded selected resource adapters, not exhaustive vendor exports or
live-account OAuth verification. Blank scopes and cross-origin cursors are refused;
downloads never forward OAuth credentials to a different origin.

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

## Source ingestion and recovery

`commontrace migrate mem0 export.json --context user:alice --dest STORE` imports
Mem0, Letta, Zep or Graphiti JSON exports as scoped external evidence. Use
`--dry-run` to inspect counts first. Prompts and core blocks become review drafts;
foreign ownership labels never replace the operator's selected scope.
`commontrace codegraph example.py --context project:work --dest STORE` parses
imports, definitions and calls without executing code. Install `commontrace[code]`
for native TypeScript/TSX parsing. Reingestion retires prior graph generations,
including removed symbols, while keeping their history.

`commontrace watch --daemon --dest STORE` polls and debounces canonical changes.
Failed index rebuilds retain the previous watermark. Document ingestion commits
each successful source separately; failed or wholly screened replacements remain
retryable and retain the prior admitted generation. Deduplication stays within a
source so independent documents retain their evidence attribution.

`commontrace page watch company/office --question 'Where is the office?'
--source FACT_ID --context project:work --dest STORE` registers a source-driven
wiki refresh. The job queue, watch loop and agent heartbeats schedule updates.
A page slug belongs to one owner scope. Each published revision binds its source
IDs, content and scopes to a signed receipt. Served pages and history recheck
source validity and forgetting; operator audit access can retain withdrawn history.

`connect --provider gmail-mailbox --resource 'label:work' --account work
--context user:alice --token-env MAIL_TOKEN --watch` polls native mailbox pages.
`drive-changes` bootstraps before consuming a change feed; completed page cursors
survive bounded runs and later failures. `github-repo` reads bounded text snapshots
at an explicit `--ref`. GitHub push configuration additionally accepts
`--webhook-secret-env`; `/v1/connectors/github/push` checks the raw-body HMAC,
repository and live branch head. Replay protection binds authenticated content,
so replacing a delivery header cannot roll back a newer snapshot.

## Operational checks

Profiles include scoped recent queries and allowed action checks. Their short TTL
cache contains candidate IDs, never causal assignments or authorization decisions.
Current/past/future query hints supplement explicit valid-time filters. Registered
local LLM callables can run without an API key; hosted providers keep their
credential requirements. Shared 429/503/529 cooldowns bound repeated provider
calls, and HTTP redirects never forward provider bearer credentials.

`COMMONTRACE_LOG_FILE` enables private rotating JSON logs for the gateway/CLI and
Hub, with credential redaction and request identity. `make test-unit`,
`make test-integration`, `make test-e2e`, `make coverage` and `make check` expose
repeatable checks. CI enforces at least 80% overall Python coverage, retains the
existing stricter fact-index gate, scans Python/TypeScript with CodeQL, and exports
an OpenSSF Scorecard and dependency SBOM. Branch Scorecards scan tracked files;
the default-branch job also evaluates repository-wide policy and history. Generated TypeScript additionally calls
all eight memory operations against a live isolated gateway in CI.

`commontrace --offline <command>` (or `COMMONTRACE_OFFLINE=1`) refuses every
non-loopback network call -- hosted model providers, connectors, crawling, Hub sync,
remote memory clients -- and loads models from the local cache only; a model runtime
on localhost stays reachable. The gateway serves `/v1/health/live` (process only)
and `/v1/health/ready` (store, schemas, free disk; 503 when not ready) without a
token. `commontrace lesson edit SLUG` opens a lesson in `$VISUAL`/`$EDITOR`, then
validates, screens and journals the change; a hand-edited active lesson returns to
review. `commontrace init --agent-caller NAME` mints a scoped agent key in one step.
