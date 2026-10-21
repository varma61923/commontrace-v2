# Measure foreign memory

Existing SDK adapters remain available for Mem0, Zep/Graphiti, Letta, AgentCore
and Claude memory stores. `VendorReadAdapter` additionally normalizes pinned HTTP
read responses for Mem0, Zep, Graphiti, Letta, AgentCore, Vertex Memory Bank,
Foundry and Anthropic memory-tool backends. Install `unimem[memory-vertex]`
(or the corresponding provider extra) for its lazy default HTTP transport.

Supply the regional/deployment retrieval endpoint, authenticated fixed scope and
transport. Cloud signing, OAuth/service-account refresh and any API-version
translation belong to that transport. These adapters do not provision cloud
resources or claim to support every vendor API version. HTTPS endpoints reject
embedded credentials; errors redact transport details. Bounded representative
response fixtures exercise normalization and stable provider/scope/item ids.
Missing vendor ids fall back to a content hash.

Wrap an adapter in `MeasuredMemory(adapter, root=...)`. Before a foreign item can
reach the agent it receives external-integrity lineage and the normal injection
screen. Canonical holdouts log exact schedule propensities and transparency
anchors; opt-in F1 harm budgets apply through `CausalMemory`. Record actual task
outcomes. `frontier vendor-report` compares per-store measured effects, with
traffic-population differences explicit. Foreign data remains quoted content;
it cannot become reviewed instructions through a provider response.

Local withdrawal is independent of deleting at the vendor. Read adapters cannot
delete remotely; an application must explicitly request supported vendor
removal. No vendor credential is stored in the measurement or lineage logs.
