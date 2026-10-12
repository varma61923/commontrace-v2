# Remove the unused in-process MCP CLI runner

Date: 2026-10-12. This is one bounded F7 simplification, not completion of the
module-count or broader subsystem-consolidation goals.

## Evidence and decision

The retained Graphify report has 286 possible unreachable callables and warns
that callbacks, dynamic dispatch and SDK exports can be missed. Several sampled
flags are demonstrably live: `agent_loop._noop_executor` is selected as a default
callable, `_HttpOnlyRedirects` is registered with urllib's opener and
`reference.build_index._safe_mtime` is used by the reference index entry point.
None of those flags supports removal.

Independent source inspection found one obsolete private path:
`mcp_server._run_cli`. The graph explanation has containment/rationale and
outgoing library calls, with no caller. Exact-name searches across implementation,
tests, scripts, Hub, SDKs, documentation, protocol and workflow sources found only
its definition. The module has no wildcard re-export, `__all__`, module-global
function-discovery registry, `globals()` dispatch or string lookup for this name.
The package exports completion wrappers; the package script points to
`commontrace.cli:main`; the serve command calls `serve`/`build_server`. Registered
MCP tools are explicitly decorated functions inside `build_server`, not all
module attributes.

Both live CLI-backed tools, `capture` and `propose_lessons`, already await
`_run_cli_async`, which runs the installed CLI in a child process through bounded
workers. The unused older helper reconstructs argparse in-process and redirects
process-global stdout/stderr. Removing it leaves the single existing execution
path and removes its otherwise-unused argparse import. All CLI and MCP command
names, arguments, result shapes, aliases and optional-SDK fallbacks remain.

Competitor inspection reinforces why static graph flags are insufficient:
mem0 `b7ad69af`, `mem0/utils/factory.py:33–36`, loads provider classes from dotted
strings through `import_module`/`getattr`; its reranker package explicitly
re-exports implementations via `__all__`. Such provider classes must survive
even when a static caller graph misses them. This removal adapts no competitor
code and does not remove any public provider/export.

```bash
graphify explain 'commontrace/mcp_server.py::_run_cli' \
  --graph /tmp/commontrace-nextgen-final-20261012b/graphify-out/graph.json
rg -n '\b_run_cli\b' commontrace tests scripts hub sdk docs protocol .github
```

The source audit and tests precede removal. Existing MCP server, progress and
worker tests exercise real capture/proposal execution, tool catalogs, old SDK
signatures, cancellation, concurrent responsiveness and approval behavior. No
new test merely asserting a private symbol's absence is added.

## Validation

```bash
./.venv/bin/python -m pytest tests/test_mcp_server.py \
  tests/test_mcp_progress.py tests/test_async_worker_boundary.py -q
```

Baseline: **95 passed in 65.02 seconds**. Candidate: **95 passed in 65.16
seconds**. Both runs emitted the existing fork-after-thread deprecation warning.
The time difference is ordinary suite duration, not a performance improvement.
Distribution and lazy CLI regressions separately reported **12 passed in 1.14
seconds**. Ruff, compilation, OpenAPI drift and whitespace checks passed.

The source decreased from **2,665 to 2,639 lines**, removing one 23-line helper,
its unused import and separator lines. Generated `docs/api.md` was refreshed
through `scripts.dev.references()` because the deletion shifted MCP source
anchors; tool signatures and descriptions did not change. The documentation
drift check then passed. No other generated references were written.

This slice makes no latency, quality or whole-project dead-code claim. External consumers
that import an undocumented private implementation helper cannot be enumerated;
supported public surfaces and repository dynamic references were inspected.
