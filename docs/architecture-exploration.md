# Explore UniMem with Graphify

[Graphify](https://github.com/Graphify-Labs/graphify) extracts a local code dependency
graph. It helps find callers and shared boundaries before changing canonical
memory, retrieval, or tenant authentication. It is a development tool and is not
a product runtime dependency.

## Install the reviewed source

Python 3.10 or newer and `uv` are required. Install the same immutable upstream
revision used by the architecture workflow:

```bash
uv tool install 'graphifyy[terraform,sql] @ git+https://github.com/Graphify-Labs/graphify.git@86c57be8204673c2a1b9849a3292a7dd3ab373d8'
graphify --version
graphify install --platform codex
```

The revision identifies Graphify 0.9.92. The Codex integration installs in the
user's skill directory; it does not change product code or install commit hooks.

## Extract code and query a boundary

Run these commands from the repository root. Choose an output directory on a
persistent volume if the execution environment clears `/tmp` between sessions.
`--code-only` and `GRAPHIFY_BACKEND=none` keep extraction local and disable LLM
backends. Documentation, images, papers, and unsupported or sensitive files are
reported as skipped; they are not part of this code map.

```bash
GRAPHIFY_BACKEND=none graphify extract . --code-only --max-workers 2 --out ../unimem-architecture

graphify explain commontrace_memory_authority_bind --all \
  --graph ../unimem-architecture/graphify-out/graph.json

graphify affected commontrace_memory_authority_permits_record --relation calls --depth 1 \
  --graph ../unimem-architecture/graphify-out/graph.json

graphify query 'canonical memory transactions' --seed commontrace_hierarchical_mutate_facts --budget 1800 \
  --graph ../unimem-architecture/graphify-out/graph.json
```

Prefer exact node IDs and a narrow seed to broad file queries, which can exhaust
the output budget on utility imports. An EXTRACTED call edge locates a source
relationship. It does not prove the call executes, that authentication reaches
all branches, or that an optional SDK works. Confirm sensitive paths in source
and run integration tests. Re-extract after implementation changes before using
the graph as evidence about the new code.

## Audit integrity and reviewed budgets

Wait for extraction to complete before consuming its output:

```bash
python scripts/graphify_audit.py \
  --graph ../unimem-architecture/graphify-out/graph.json \
  --output ../unimem-architecture/audit.json \
  --source-root . --max-modules 397 --max-unreachable 318

graphify diagnose multigraph --json \
  --graph ../unimem-architecture/graphify-out/graph.json
```

The 2026-10-18 extraction contains 397 Python modules and 294 unreachable
candidates (zero missing edge endpoints), and the checked-in budget is set to
those measured values. The workflow checks missing edge endpoints and reviewed
module and reachability budgets. Its unreachable candidates require inspection: callbacks, SDK exports,
and generated entry points can be absent from a static call graph. Self-loops
can reflect recursive calls or imperfect name resolution; inspect them before
calling them defects. A successful graph audit does not replace type checks,
security analysis, or tests.

Keep generated graphs outside the product tree unless the team deliberately
chooses to version them. Graphs contain code identifiers and paths. Large graphs
can be explored through scoped CLI queries without loading a full visualization.
