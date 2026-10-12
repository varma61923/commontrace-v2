# First local recall smoke

The shortest keyless local path is the installed CLI: initialize a store, add an
explicit fact, then recall it. It uses the base `commontrace` package and needs no
model credentials, optional extras, or running service. Run the disposable-store
check with:

```bash
python -m pip install -e .
python scripts/first_recall_smoke.py
```

The harness sets `COMMONTRACE_OFFLINE=1`, creates and removes its own temporary
store, and fails unless recall returns the exact fact it added. `--temp-dir` can
point the disposable store at a chosen scratch filesystem. It does not modify the
project's `memory/` directory.

## Local measurement

On 2026-10-12, a clean Python 3.12.3 virtual environment installed the local
checkout with `pip install -e .`, initialized a temporary store, added the sample
fact, and recalled it successfully. The run took **12.59 seconds** from virtualenv
creation through verified recall; **8.96 seconds** elapsed from the start of
`pip install` through recall. The measured command times were 8.149 seconds for
installation, 0.164 seconds for initialization, 0.345 seconds for the fact write,
and 0.305 seconds for recall. Virtualenv creation took 3.626 seconds. The install
used the cached PyYAML wheel and base dependency set. The scratch store lived under
`/dev/shm` and was removed after the run.

This is a local workflow smoke, not release acceptance: it was one warm-cache run
while unrelated benchmark work was active. It confirms the first-value path and
shows this environment below the 60-second target, but it does not establish a
cold-cache or cross-platform installation bound. The harness reports operation
times after installation; use a fresh virtualenv when repeating the full install
measurement.

## Empty-store setup check

Also on 2026-10-12, `commontrace up --dest <temporary-project> --setup-only
--agent codex` initialized a fresh store and installed the project-local Codex
hooks in **0.21 seconds**. Its result had `first_lesson: null` and
`first_value_status: "waiting for an evaluated run or reviewed import"`. This
confirms the setup path is fast, but it does not meet the separate goal of
showing a first injected lesson from an empty store within two minutes. The
current behavior avoids inventing a lesson; a safe first lesson still needs an
evaluated run or source material the user has reviewed.

Reproduce the setup-only check with:

```bash
tmpdir=$(mktemp -d)
commontrace up --dest "$tmpdir" --setup-only --agent codex
rm -rf "$tmpdir"
```

The measurement used a temporary directory under `/dev/shm`; the generated
project and hook files were removed with that disposable directory.

## Competitor onboarding patterns inspected

The local Hindsight README leads with a Docker service plus an LLM API key, or a
bare-metal API process configured with a provider, followed by SDK retain/recall
calls. The local Mem0 README offers a compact cloud-backed CLI sequence and a
Python library flow; its Python quickstart requests an OpenAI key. CommonTrace's
local first-value check keeps the same concrete add/search shape while using its
local store and offline recall path.

Inspected local snapshots: Hindsight `a9eeed77f9bce99d8f97d39a30d3281747300824`
and Mem0 `b7ad69afda6b6ed030347c66d48a13e4de9dec08`.

## TypeScript local npm package smoke

The TypeScript SDK now has a disposable end-to-end smoke:

```bash
python3 scripts/typescript_first_recall_smoke.py
```

It copies `sdk/typescript` to a temporary directory, installs SDK development
dependencies, runs `npm test`, packs the package, then installs that tarball
into a separate temporary consumer with a fresh npm cache. It initializes a
temporary CommonTrace store, disables holdout only in that store so the one-shot
check is deterministic, starts the local gateway on an ephemeral loopback port,
and imports `MemoryClient` from the installed package. The SDK adds one explicit
fact in local mode and verifies that `reflect()` returns that exact fact. The
npm caches, build output, store, gateway process and consumer install are
removed when the smoke exits.

On 2026-10-12, Node `v18.19.1` and npm `9.2.0` passed. From the start of the
consumer's `npm install` through the first verified reflection took **7.94 s**:
3.37 s for npm install, 0.37 s for store setup, 0.25 s for gateway startup and
3.95 s for SDK add plus reflect. The full harness took **23.52 s**, including
SDK dependency installation, build and tests, and package creation. This was one
run with a fresh temporary npm cache; it does not establish a cross-platform or
repeated-run bound.

The package smoke caught an export-path defect that source-imported tests missed:
`tsc` writes the entry point to `dist/src/index.js` and declarations to
`dist/src/index.d.ts`, while the package had pointed to `dist/index.*`. The
package `main`, `types` and `exports` now match the compiler output; the packed
consumer import and runtime smoke pass with that metadata.

Graphify was used to trace the CommonTrace TypeScript package toward
`MemoryClient` and the local gateway, and its Mem0 graph surfaced the Node SDK
quickstart. The inspected competitor flows are intentionally short: Mem0's Node
guide installs `mem0ai`, constructs `MemoryClient` with an API key, then calls
`add` and `search` (`mem0/skills/mem0/client/node.md`); Hindsight's TypeScript
README installs `@vectorize-io/hindsight-client`, constructs a client with a
base URL, then calls `retain` and `recall`
(`hindsight/hindsight-clients/typescript/README.md`). This CommonTrace smoke
adopts the same add/read sequence while exercising the local gateway and local
store.

This is a **local npm tarball** measurement, not a registry install. It does not
publish the package and does not claim that `npm install @commontrace/hub-client`
works from a registry. The server is a local CommonTrace checkout; the smoke
does not measure installing or launching the Python server for a Node-only
consumer.

## Local Docker image smoke

On 2026-10-12, Docker client/server **29.1.3** built the `Dockerfile.local`
runtime and recalled one exact fact from a disposable named volume with
`COMMONTRACE_OFFLINE=1` and `--network none`. The first invocation initialized
the store and added the fact; the second invocation returned it in JSON. From
starting the first container through parsing and verifying the recalled text
took **2.077 seconds** (add container: 1.227 s; recall container and check:
0.851 s). This timer includes two container starts and first-store
initialization. It excludes image build/pull and volume creation.

The measured fact was `The rollback contact alias for project Kestrel is
cedar-owl-417.` and the query was `What is the rollback contact alias for
project Kestrel?`. Reproduce the container calls against a disposable volume
after building `commontrace-local:target-g-smoke`:

```bash
docker volume create ct-target-g-recall-20261012
docker run --rm --network none -e COMMONTRACE_OFFLINE=1 \
  -v ct-target-g-recall-20261012:/data commontrace-local:target-g-smoke \
  fact add 'The rollback contact alias for project Kestrel is cedar-owl-417.'
docker run --rm --network none -e COMMONTRACE_OFFLINE=1 \
  -v ct-target-g-recall-20261012:/data commontrace-local:target-g-smoke \
  recall 'What is the rollback contact alias for project Kestrel?' --json
docker volume rm ct-target-g-recall-20261012
```

The timer above began immediately before the first `docker run`, not at build or
pull. It is a single local, prebuilt-image run; it does not measure the cold
image download/build, Compose startup, gateway/MCP readiness, or a production
deployment. The test image and named volume were removed after the run.

This Docker installation has no BuildKit/buildx component (`docker buildx` is
unavailable and `DOCKER_BUILDKIT=1 docker build` reports the missing plugin).
The legacy builder also ignores Dockerfile-specific ignore files. A normal
repository-context build first rejected `COPY --chmod`; `Dockerfile.local` now
uses a portable `COPY` followed by `RUN chmod`. The next legacy build then
revealed that the repository's Hub `.dockerignore` omitted CLI modules required
by the local image. For measurement, the build used a temporary isolated
context containing the package inputs from `Dockerfile.local.dockerignore`,
with that ignore file copied to the context root as `.dockerignore`; the
temporary context was deleted afterward. The reproducible helper now creates
this context directly from a fixed allowlist, without relying on either ignore
file. It rejects symlinks in included source trees, omits generated Python
bytecode, and removes the temporary context after successful or failed builds.
It leaves the Hub `.dockerignore` and checkout untouched:

```bash
python scripts/build_local_image.py --tag commontrace-local:target-g-smoke
```

With BuildKit installed, a direct repository-root build remains available
because Docker honors the Dockerfile-specific ignore file:

```bash
docker build -f Dockerfile.local -t commontrace-local:target-g-smoke .
```

The first helper-driven build on this host (Docker 29.1.3, 2026-10-12) staged
the context successfully, then stopped before any build layer because Docker Hub
rejected the pinned Python base-image pull at the unauthenticated rate limit.
No image or volume was created, and the temporary context was removed. The
offline add/recall smoke therefore remains the earlier successful measurement;
this helper run did not repeat it.
