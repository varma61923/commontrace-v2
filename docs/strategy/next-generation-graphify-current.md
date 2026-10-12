# Current Graphify audit

The 2026-10-12 code-only Graphify scan contains 21,807 graph nodes and reports
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
