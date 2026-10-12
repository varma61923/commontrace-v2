# Supermemory local benchmark adapter: no-go

Inspected `/root/Test/supermemory` at revision
`709723dea69dba8340fc42c0aafd86a2b6a4a0f4`. The public docs expose a plausible
raw-source retrieval path: `apps/docs/concepts/super-rag.mdx` says
`taskType: "superrag"` skips fact extraction, profile updates, and graph
linking while retaining chunk/embed/index and `searchMode: "chunks"` recall.
The local overview says those API routes are available on the local server and
the default embedding provider runs locally. This is a better fit than the
legacy v4 direct-memory endpoint, which writes memory objects rather than
source documents.

No adapter was added. The self-hosted server executable is a downloadable,
non-public binary (`apps/docs/self-hosting/overview.mdx`); the supplied checkout
does not include a versioned server package or reproducible local launcher.
The quickstart says state and model cache share `SUPERMEMORY_DATA_DIR`,
credentials live under `~/.supermemory/env`, and first boot may invoke an
interactive provider setup. The configuration docs require an LLM provider
for the broader service, document telemetry as enabled unless
`SUPERMEMORY_DISABLE_TELEMETRY=1`, and do not document a way to seed or verify a
model cache separately from service data. The local server's v5 direct-memory
create route is also explicitly absent in `apps/docs/recall/memory-operations.mdx`.
Although `supermemory-server` is present on this machine's PATH, I did not run
it: the checkout does not pin that binary or document a reproducible isolated
launch, and startup could initialize persistent state or load models.

An adapter pointed at an already-running server could write benchmark data into
the user's persistent database, inherit remote provider credentials, or fetch
models without the adapter being able to prove otherwise. Loopback URL checks
and a temporary namespace would not isolate the database, model cache,
credentials, or telemetry. No native Supermemory score was measured, and no
service writes, package installs, or model downloads were attempted.

Reopen this lane when a pinned local-server executable and documented launch
contract are available that let the adapter own a temporary `HOME` and
`SUPERMEMORY_DATA_DIR`, disable telemetry, use a pre-seeded local embedding
cache without fetching, configure all providers locally, and create a fresh
temporary API credential. Then exercise `taskType: "superrag"` plus
`searchMode: "chunks"` against that isolated instance, verify source identity
and cleanup, bind the exact server and adapter revisions, and only then run a
native benchmark.
