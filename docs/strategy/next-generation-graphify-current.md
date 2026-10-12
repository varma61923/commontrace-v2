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
with SHA-256 `222cdbebd4234f8e26ed64cdd5ed1333924f604ffd722205534ee83fea484653`.
The source graph had SHA-256
`2b7c25025652bba16bbf3b1ee2f481dd6f517edde278cd2ae936766a457b6219` and was
extracted from the source tree that includes the framework and extraction
changes in this release. Its code state is commit
`a6fe848cf484d46c5444c8b1c5764f72b168e822`; no code changed between extraction
and that commit.
