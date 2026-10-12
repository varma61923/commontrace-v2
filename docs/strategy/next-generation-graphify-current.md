# Current Graphify audit

The reviewed baseline 2026-10-12 code-only Graphify scan contains 21,807 graph nodes and reports
300 Python modules, 287 possible unreachable callables,
zero dangling edge endpoints, and zero policy violations. It remains inside the
reviewed CI budgets of 306 modules and 303 reachability candidates. The 287
callables are candidates for human review, not proven dead code: static analysis
does not resolve all dynamic dispatch, SDK exports, callbacks, or generated
entry points.

Reproduce from the repository root:

```sh
graphify extract "$PWD" --code-only --no-cluster --out /tmp/commontrace-graphify-current
python3 scripts/graphify_audit.py \
  --graph /tmp/commontrace-graphify-current/graphify-out/graph.json \
  --output /tmp/graphify-audit.json --source-root . \
  --max-modules 306 --max-unreachable 303
```

The committed audit report is [measurements/graphify-audit-2026-10-12.json](measurements/graphify-audit-2026-10-12.json)
with SHA-256 `b75e6177edf5720c13a6e03cc41b6bff2e051a5da78b69c9adf49ce5f818b714`.
The source graph had SHA-256
`9d4ca349c5eb278887fb59f6ff30bd44ee710b05336221d96ed41d0bab3e1d76` and was
extracted from the source tree that includes the framework and extraction
changes in this release. Its code state is commit
`afa5369837730960c7df9c85e913bb7d665d7b33`; no code changed between extraction
and that commit.

## Current working-tree refresh (2026-10-12)

Graphify 0.9.82 scanned the current dirty worktree in code-only mode into a
temporary directory, without overwriting the retained `graphify-out/` tree.
The refreshed graph has 22,047 nodes and 68,065 edges. The audit reports 300
Python modules, 286 possible unreachable callables, zero dangling endpoints
and no budget violations. The earlier same-worktree scan had 287 candidates
and the same 300 modules; shared CLI remediation reduced that list to 286, and
the later Graphiti-inspired entity index and EverOS-inspired lesson selection
kept the current count at 286. The `remediation.hint` compatibility path
remains. This is a reachability signal, not proof that any candidate is dead.

Graphify's `path` command confirms `cli.main()` now calls
`exceptions.remediation_hint()`. The old inferred edge from two unrelated test
parameters named `hint` did not represent a call to `remediation.hint`; the
targeted repository search found no such test invocation. The measurement
records the full limitations and hashes in
[the working-tree report](measurements/graphify-audit-working-tree-2026-10-12.json)
with SHA-256
`a9b713011974ff5111fef865b2aacc1ab4e113b53eb7f5315c5b921864af4839`. The
current graph SHA-256 is
`e9784cb4839bfb635627145b048ab12b9c1f0e7daf13eb417e6201eacbd0c12c`.

Reproduce with:

```sh
graphify extract "$PWD" --code-only --no-cluster --out /tmp/commontrace-nextgen-final-20261012b
python3 scripts/graphify_audit.py \
  --graph /tmp/commontrace-nextgen-final-20261012b/graphify-out/graph.json \
  --output /tmp/commontrace-graphify-audit-final-b.json --source-root "$PWD" \
  --max-modules 306 --max-unreachable 303
```
