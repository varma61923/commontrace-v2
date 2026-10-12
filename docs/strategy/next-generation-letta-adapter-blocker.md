# Letta benchmark adapter assessment

**Decision (2026-10-12): defer a Letta profile.** The supplied
`/root/Test/letta` checkout is on `main` at
`5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a` and contains the landing-page tree,
not the current Letta runtime or SDK. Its `AGENTS.md` explicitly prohibits
using its retired V1 server, old packages, or archive
branch for benchmarks and directs programmatic benchmarks to the current Letta
Agent SDK and App Server. No `letta-code` checkout is present under `/root/Test`
or `/root/dev`.

The current docs describe a local Agent SDK/App Server for running persistent
agents, with the App Server owning agent execution and state. They also
document REST archival passage create/search operations, but the Python API
library page marks that client deprecated and directs new development to the
Agent SDK. The available documentation does not establish that the passage
operations are supported by the current local Agent SDK/App Server, or provide
a raw-source ingestion and recall contract that avoids agent-driven extraction
or generation. Reusing the legacy Python client or assuming those REST paths
work against current local App Server would therefore risk benchmarking a
retired or unverified integration.

No adapter, shim, dependency install, or native score was added. Reopen this
after the current `letta-ai/letta-code` source or an explicitly supported,
pinned current SDK contract is available, and verify a local path that stores
source turns as-is and returns native ranked turns without an LLM extraction
stage. The eventual CommonTrace integration point is `PROFILES` plus the
`vendor_profile()` factory in `benchmarks/vendor_adapters.py`; an adapter must
implement the existing `retrieve(question, budget)`, descriptor, and `close()`
contract so the common benchmark continues to own corpus cutoff, evidence
mapping, answer judging, and context packing. If the implementation lives in a
separate helper module, include that helper's SHA-256 in the descriptor and
check it before and after measurement; the current runner only binds the
`vendor_adapters.py` source hash.

References:

- [Letta repository guidance](https://github.com/letta-ai/letta/blob/main/AGENTS.md)
- [Current Agent SDK deployment modes](https://docs.letta.com/agent-sdk/deployment)
- [Current App Server responsibilities and local mode](https://docs.letta.com/self-hosting/app-server)
- [Archival passage API](https://docs.letta.com/api/python/resources/agents/subresources/passages)
- [Python API client deprecation notice](https://docs.letta.com/api/python)
