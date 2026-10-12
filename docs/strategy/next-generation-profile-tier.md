# F6 profile tier: turn acceptance and gap

## Finding

CommonTrace already captures literal first-person preferences into profile
records and reads `static`, decayed `dynamic`, and `custom` buckets in
`MemoryClient.turn`. The same operation returns ranked memory `results`. The
gateway exposes it as `POST /v1/memory/turn`, so callers can receive both in a
single request without changing the profile shape.

The integration gap was the turn's search guard: it ran `search_recipes.search`
only when `fact_store.enabled(root)` was true. On a fresh store that has
authoritative facts but has not migrated to the SQLite fact index, ordinary
`MemoryClient.search` falls back to the hierarchical reader and finds them,
while `turn` returned an empty result list. The turn now delegates every
non-empty query to `search_recipes.search`; that function already selects the
indexed or hierarchical backend. Empty queries still skip search.

## Focused evidence

`tests/test_profile_search_acceptance.py` seeds a local fact and a scoped
custom profile record, then sends one request through `Gateway.handle` to the
production `/v1/memory/turn` route. Its response must include the automatically
captured static and dynamic preferences, the custom bucket, and the seeded
fact in `results`. The test was run before the change and failed only on the
missing search hit; after the change it passes. This is a functional acceptance
check, not a latency measurement.

Graphify's CommonTrace map contains entry nodes for `MemoryClient.turn` and
`framework_middleware.turn`; source inspection follows the client's dynamic
import to the middleware and its search call. The Supermemory Graphify map
contains `getProfileWithSearch`. Its current
`packages/tools/src/tools-shared.ts::getProfileWithSearch`
composes `client.profile` and `client.search` as two parallel requests; the old
single-call example in `apps/docs/install.md` should not be treated as a current
server-latency result. Cognee's Graphify map contains `effective_weight` and
`load_preference_weights`; source inspection shows decay toward neutral by turn
age, applied during lookup. CommonTrace already decays the turn profile's
dynamic weights by `half_life_days` on read.

## Scope and remaining work

This slice repairs search results for the existing combined turn API. A
follow-up now uses effective user-authored dynamic preference weights to
rerank only the bounded result list; see
[the preference-ranking note](next-generation-profile-preference-ranking.md).
The separate `MemoryClient.profile()` operation remains a profile-only
endpoint. The test exercises the in-process gateway request handler and
production turn path, not a listening HTTP server. No request-latency or
competitor-latency claim is measured here. It does not evaluate profile
extraction quality, always-inject policy, preference-ranking uplift, or
large-store behavior.
