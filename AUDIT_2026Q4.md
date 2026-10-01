# Audit 2026 Q4

Scope of this pass: the paths a customer or an attacker can reach (agent-facing
retrieval, the customer console, the operator console, SCIM, the billing
webhook, API-key lifecycle). Method: read the code, write the failing test
first, fix, keep the test. Every fix below has a regression test that was seen
to fail before the change. This is not a penetration test and not a SOC 2
audit; it lists what was examined, so what was not is visible.

## Findings

| # | Sev | Finding | Status | Regression test |
|---|-----|---------|--------|-----------------|
| 1 | Medium | Lesson text was screened for prompt injection only at admission. An active lesson edited on disk afterwards, or hand-written, reached the agent unexamined. | Fixed: `injection_guard` screens at `retrieve` and `query`, quarantines before dosage and arm assignment (so no occasion is logged as treated for a lesson the agent never saw), and `retrieve` carries a notice that lessons are reference material. | `tests/test_injection_guard.py` |
| 2 | Medium | A non-UUID id in a console path (`users/`, `keys/`, `alerts/`, `webhooks/`) reached Postgres and raised a driver error: a 500. | Fixed in `refuse_cross_origin`: 404, same as an unknown id. | `hub/tests/test_cross_tenant_fuzz.py` |
| 3 | Medium | `rotate_api_key` issued the successor with no scopes, which grants every scope: rotating a read-only workload token produced an admin token. | Fixed: successor carries the old key's scopes. | `hub/tests/test_auth.py::test_rotating_a_narrow_key_does_not_widen_it` |
| 4 | Medium | `rotate_api_key` locked nothing: six concurrent rotations of one key minted six live keys. | Fixed: `SELECT ... FOR UPDATE` on the old row. | `test_concurrent_rotations_of_one_key_mint_exactly_one_successor` |
| 5 | Medium | A revoked key could be rotated back to life, defeating revocation as the way to end a compromised key. | Fixed: refused; console and operator console answer "not rotated". | `test_a_revoked_key_cannot_be_rotated_back_to_life`, `test_rotating_a_revoked_key_from_the_console_changes_nothing` |
| 6 | Low | Two simultaneous deliveries of one billing event: the loser raised `IntegrityError` (a 500) and the sender kept retrying. Nothing applied twice (the loser rolled back). | Fixed: 200 once the ledger confirms the event is recorded. | `hub/tests/test_billing.py::TestConcurrentReplay` |
| 7 | Low | `hub/bench_concurrency.py` crashed in its cleanup after a memory-only run and leaked its org and keys. | Fixed. | run manually (`--backend memory`) |
| 8 | High | Nine limiters were built as process-local `RateLimiter`s directly: console sign-in, signup (form and REST), share-link views, the operator console, connector, OTLP and REST auth, and `/readyz`. Under `HUB_RATE_LIMIT_BACKEND=postgres` each replica kept its own budget, so the sign-in guard of 5 attempts per source became 5 x replicas. | Fixed: every limiter is built by `abuse.make_named_limiter` on the configured backend, under its own name; a source scan fails if a route constructs one directly. | `hub/tests/test_shared_limiters.py` |
| 9 | Medium | After an IdP rotated its signing key, every token carrying the new `kid` failed until the cached JWKS expired: up to an hour of SSO lockout per routine rotation. | Fixed: an unknown `kid` refetches the JWKS, at most once a minute per URI however many unknown kids arrive. | `hub/tests/test_sso.py::TestKeyRotation` |
| 10 | Low | The verification key was built from the JWK's own `alg`, so the document rather than the allowlist decided the key type; an encryption key (`use: enc`) was accepted for signatures. | Fixed: the key is built for the token's allowlisted algorithm; `kty`, any declared `alg`, and `use` must agree. | `hub/tests/test_sso.py::TestTheJwkMustFitTheTokensAlgorithm` |
| 11 | Medium | A Proof share link could not be revoked: a link forwarded too widely stayed live for its full 14 days. | Fixed: links carry the org's share generation; an admin's "Revoke all share links" bumps it (audited), and older links answer 404. Links for a deleted org also 404. | `hub/tests/test_console.py::TestShareLinks` revocation tests |
| 12 | Medium | `X-Forwarded-For` was read from its first header line only, so a client-sent line won when a proxy added a second line; and a proxy that writes `IP:port` made every connection a fresh rate-limit bucket. | Fixed: all lines are read in order; a port is stripped before keying. | `hub/tests/test_abuse.py::TestForwardedForCannotBeSpoofed` |
| 13 | Medium | Memory from external stores (Mem0, Zep, Letta, Claude memory stores, AgentCore) was measured but never screened for prompt injection. | Fixed: `MeasuredMemory` screens before arm assignment (quarantined memories are never logged as treated or withheld) and reports `quarantined` reasons by pattern name only. `CausalMemory(screen=True)` opts a bare wrapper in. | `tests/test_memory_adapters.py::TestExternalMemoryIsScreenedForInjection` |

## Checked and found sound

* Billing webhook: signature, 5-minute timestamp window, event-id ledger (sequential replay).
* Console and crud: org-scoped lookups. Foreign and hostile trace ids through `get_trace`, `delete_trace`, `vote_trace`, `tag_trace_subjects`, `amend_trace` return not-found and change nothing (`hub/tests/test_crud_id_fuzz.py`).
* Every console id route, as org A against org B's user, key, alert rule and webhook endpoint: no change, no leak. A route with an unmapped path parameter fails the test, so a new route cannot ship unfuzzed.
* Operator console and SCIM: no 5xx for hostile ids on any verb.
* Every Hub MCP tool is registered through `scoped_tool` (existing `test_api_key_scopes.py`).

## Open

* **Injection screen is pattern matching** with the limits `memory_guard` documents; an attacker who phrases around the patterns is not stopped. It now covers lessons and external memories alike.
* **Image scan runs in CI, not here.** `docker-build` scans the built image with Trivy (pinned by digest; fails on a fixable CRITICAL, prints HIGH). The Docker daemon is unavailable where this pass ran, so its first real result is CI's.
* **Not examined:** an interactive OIDC authorization-code flow, because there is none: the Hub accepts bearer JWTs only (`hub/sso.py`), which findings 9 and 10 cover.

## Closed since the first pass

* **MCP over HTTP with two live keys:** CI's `compose-stack` job runs `hub.smoke` against the built image with two orgs' keys and checks that `get_trace`, `vote_trace` and `amend_trace` across the boundary answer `not_found`, and that one org's trace never surfaces through the other's `commons_overlap`.
* **Concurrent key issuance against a plan's key limit:** plans carry no key limit, so there is nothing to race. The limits that do exist (`max_traces`, `max_agents`) re-check under `SELECT ... FOR UPDATE` on the org row.
* **Share-token format, header spoofing, SSO verification:** findings 9-12.
