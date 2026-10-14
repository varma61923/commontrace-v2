# Next-generation feature completion

This release closes the actionable gaps in the supplied competitive report.
The [54-item acceptance matrix](research/next-generation-acceptance.md) distinguishes
existing capabilities, new implementation, and the account configuration needed
for package publication. The report's percentage estimates are not measured
results. CommonTrace keeps its existing public interfaces and lean local runtime.

## Retrieval and conversation capture

Recall defaults to the locally cached FastEmbed BGE model when available, with
lexical retrieval when optional dependencies or cached weights are missing.
Installing an embedding extra does not silently download model weights. Semantic
vectors are persisted by content hash, fused with lexical evidence through RRF,
and scoped before graph traversal, reranking and context allocation.

To explicitly prepare vectors while adding messages:

```bash
commontrace conversation add --help
commontrace conversation add support --session customer-42 \
  --file conversation.json --cache-vectors --embedder fastembed:BAAI/bge-small-en-v1.5
```

Use the normal command's `--dest` flag to select the store. The equivalent
`POST /v1/conversation/add` accepts `cache_vectors: true` and an optional `embedder`
tag; these fields are in the generated OpenAPI contract. Provider tags and installed dependencies are
checked before insertion; missing cached weights degrade cache readiness. Redacted source messages commit first; inference runs
in bounded batches afterward. Provider failures report `embedding_cache.ready:
false` and a generic error type, retaining the source for sparse recall and later
index preparation. The default capture path performs no extra model calls.
Previously cached content is reused. Requested embedding dimensions are validated,
and old vectors with incompatible dimensions are rebuilt.

Future plans retain the observation time as their assertion time and the future
occurrence date in their text. A planned job or move occupies a separate `plan:`
belief slot, preventing it from superseding the current job or home. Point-in-time
belief filtering and graph `valid_at`/`invalid_at` histories remain available.

## Configured providers

```bash
python -m pip install 'commontrace[providers]'
commontrace providers list
commontrace providers schema llm
commontrace providers validate llm model.json
commontrace help "semantic search"
```

The optional Pydantic configurations reject unknown fields and unsafe endpoint
URLs, hide inline secrets in representations and public exports, and support
`api_key_env` and `dsn_env` secret references. Validation does not resolve secrets
or contact a provider. Factory construction resolves explicitly selected secrets.
Trusted application code can create independent configured model instances:

```python
from commontrace.providers import LLMFactory

model = LLMFactory.create("openai", {
    "model": "gpt-4.1-mini",
    "api_key_env": "TEAM_OPENAI_KEY",
})
text, usage = model.complete("Summarize this incident using only the supplied evidence.")
```

Native routes cover OpenAI-compatible services, OpenAI, Anthropic, Gemini, Ollama,
local models, Bedrock and Vertex. Embedding and vector factories preserve the
existing provider interfaces; vector creation pins owner, dataset, model and
dimensions and refuses a backend that changes them. Legacy dataclass/string
configuration still works without installing Pydantic. Structural protocols
provide the abstract interfaces without forcing third-party inheritance.

## Discoverable CLI and guided workflows

Top-level help groups commands by purpose. `commontrace help TOPIC` searches
command names, descriptions and aliases; `--json` returns discovery metadata.
Shortcuts are `add` → `fact add`, `search` → `recall`, `check` → `doctor`, and
`summarize` → `conversation summarize`.

```bash
commontrace setup
commontrace setup --workflow runtime --dest ./memory-store --non-interactive --dry-run --json
commontrace setup --workflow ingest --source ./documents --context team:support \
  --dest ./memory-store --non-interactive --apply
commontrace setup --workflow migrate --vendor mem0 --source export.json --context user:alice \
  --dest ./memory-store --non-interactive --dry-run
```

The wizard validates a complete plan before any write, calls existing CLI handlers
with argument arrays, and stops after a failed step. Preview makes no store changes;
interactive execution requires the user's choice at the terminal. Runtime setup
prepares configuration without starting a server. Setup, provider configuration
and server startup remain terminal-only in the web command center. Console and
wizard output buffers are bounded while commands run.

## Console history

The existing CSP-safe console now includes:

- **Lesson library:** paginated lessons across active, review, draft and archived
  states, linked to the existing evidence, revision and approval workflow.
- **Trace history:** metadata pages and redacted source inspection, with bounded
  display and no HTML interpretation.
- **Benchmark history:** accessible charts, exact saved values, and adjacent
  percentage-point deltas from `commontrace bench --save`. Missing or invalid
  scores remain unavailable; no scores are invented.

These views extend the existing no-build dashboard rather than adding a second
Next.js application. New operator APIs are `/v1/traces`, `/v1/trace?file=...`, and
`/v1/benchmark-history`. Agent credentials and container-scoped requests cannot
access global history. File access rejects symlinks, nonregular files, traversal
and oversized bodies. Scans stop at 10,000 entries and report truncation; page
limits are at most 200. This is a bounded browser view, not a replacement for
archive export. Existing governance and lesson approval preconditions remain.

## Schema and data migration

Use the migration owner's database credentials through `HUB_DATABASE_URL` or its
existing secret reference mechanism. Never give that role to a serving process.
The commands print revision identifiers and safe errors, not connection URLs.

```bash
python -m hub.migrate status
python -m hub.migrate plan
python -m hub.migrate upgrade                 # preview only
python -m hub.migrate upgrade --apply
python -m hub.migrate verify                  # head and model/schema drift
python -m hub.migrate rollback --revision REV # preview only
```

An applied rollback also requires `--allow-data-loss --backup-verified`: the latter
is an operator attestation that a separately restored backup passed a restore
drill, not an automated claim that a backup is valid. Quiesce application writers,
inspect the downgrade, and preserve a restore-tested backup before applying it.
A PostgreSQL session lock prevents simultaneous operators across Alembic commit
boundaries, including concurrent index creation. Alembic controls its revision
transactions; a migration chain is not globally atomic. After a failure, inspect
`status`, reconcile any concurrent index artifact, and retry from the recorded
revision. The lock is released and engines are disposed on failure. Alembic upgrades/downgrades remain supported.

Vendor data imports already cover Mem0, Letta, Zep, Graphiti, Cognee, Supermemory,
Hindsight and EverOS through `commontrace migrate VENDOR FILE`. Preview with `--dry-run`,
provide explicit context labels, and inspect imported records before use. Import
receipts make identical replays idempotent; imported lessons enter review. These
are conversions of supported export shapes, not remote vendor database migrations.
See [enterprise readiness](enterprise-readiness.md) for safe local backups and
[distribution](distribution.md) for deployment roles and marketplace setup.

## Generated TypeScript release

All SDK changes originate in OpenAPI and upstream Swagger-compatible OpenAPI
Generator. Generate the complete local/HTTP gateway API with:

```bash
python scripts/export_openapi.py
python scripts/generate_sdks.py --api gateway --languages typescript \
  --package-version 2.0.0 --output build/release-sdk
cd build/release-sdk/typescript
npm install --ignore-scripts
npm run build
npm pack
```

The canonical generated directory is `sdk/generated/typescript` by default,
not a duplicated `commontrace-ts` tree. Types and operations follow the same
server contract used by Python callers. Configure `Configuration.basePath` to a
local loopback gateway or HTTPS deployment and supply bearer authorization. No
new SDK implementation is handwritten. Parameter interface prefixes and named
measurement models prevent generator collisions across the full gateway API.

The release workflow builds, audits, live-smoke-tests and packs `@commontrace/sdk`
with the project version, then publishes through npm trusted publishing with
provenance. The packed tarball can be installed locally before publication.
Public npm availability requires ownership of the package scope and registration
of this repository's `release.yml` as its trusted publisher. Configured account
ownership and a successful registry publication are external acceptance criteria;
a passing package build does not establish publication. Configure release
environments as described in [release automation](../.github/workflows/release.yml).
