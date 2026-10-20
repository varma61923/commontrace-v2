# UniMem dependency radius and source diversity

`contagion.record_read` records admitted input memories, an agent, an episode and
an output. These are instrumentation claims, not proof that an uninstrumented
model consumed those inputs. Read payloads inherit source scopes, expire after
1–30 days, and remain separate from signed scope/source commitments. Retries
preserve the original deadline. An operator runs `contagion.prune` to delete
expired payloads; signed commitments remain auditable. Archive a scoped graph
namespace when its bounded artifact journal fills.

```sh
commontrace lineage contagion fact-id --context team:release
commontrace lineage contagion fact-id --context team:release --freeze incident-17
```

The second command requires operator authority and creates a frozen incident
with a commitment to the radius. It establishes a legal hold; release it through
the existing incident workflow when appropriate. The `contagion_radius` MCP
tool exposes the same scoped traversal without freezing anything.

The graph joins authenticated write lineage, managed artifact dependencies and
live read edges. It includes observed agents, episodes, outputs, derived memories,
skills, exports and capsules. Admission happens before a node can enter the
visible graph. Traversal has a maximum of 2,000 nodes and 16 levels; responses
explicitly flag truncation. Oversized underlying graphs fail closed and require
a narrower governed store. Missing instrumentation remains outside the radius.

The gateway's `POST /v1/recall` accepts optional `independence: true`. Its additive
annotation describes only admitted, delivered canonical IDs. Arbitrary external
candidate text cannot manufacture authenticated roots. Optional
`read_graph: {"output":"answer-id","retention_days":7}` records those deliveries
when `agent_id` is supplied. Neither option is enabled by default.

`n_eff` is the inverse concentration of lineage-root weights. Each memory splits
one unit of weight equally across its roots. Five summaries from the same root
therefore have `n_eff = 1`; multi-root summaries do not each become a new
independent source. Unknown roots share one conservative group. This is a
**lineage diversity estimate**, not proof that organizations, facts or errors
are statistically independent.

`contagion.independence(..., downweight=True)` provides inverse-root-frequency
agreement weights. `contagion.reretrieve` optionally invokes an independent-source
retriever below a configured threshold, then runs every proposed ID through
canonical admission again. It reports whether the threshold was actually met;
the absence of an independent source is never replaced with a fabricated one.
