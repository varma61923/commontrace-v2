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

## Checked and found sound

* Billing webhook: signature, 5-minute timestamp window, event-id ledger (sequential replay).
* Console and crud: org-scoped lookups. Foreign and hostile trace ids through `get_trace`, `delete_trace`, `vote_trace`, `tag_trace_subjects`, `amend_trace` return not-found and change nothing (`hub/tests/test_crud_id_fuzz.py`).
* Every console id route, as org A against org B's user, key, alert rule and webhook endpoint: no change, no leak. A route with an unmapped path parameter fails the test, so a new route cannot ship unfuzzed.
* Operator console and SCIM: no 5xx for hostile ids on any verb.
* Every Hub MCP tool is registered through `scoped_tool` (existing `test_api_key_scopes.py`).

## Open

* **Container scan not run.** The Docker daemon is unavailable in the environment this pass ran in, and an unverified CI step is worse than none. Add an image scan to CI once it can be exercised.
* **Injection screen is pattern matching** with the limits `memory_guard` documents; an attacker who phrases around the patterns is not stopped. Text from external memory stores (`memory_adapters`) is measured, not screened: `CausalMemory` returns the caller's own item objects, and screening there would change what is withheld. Screen at the point the caller renders it.
* **MCP over HTTP** was exercised at the crud layer (where the org filter lives), not through the transport with two live API keys.
* **Key-rotation races** are covered for one key; concurrent `issue_api_key` against the plan's key limit is not examined.
* Not examined: SSO/OIDC callback handling, the share-token format, rate-limiter bypass via header spoofing, dependency CVEs beyond the `pip-audit` already in CI.
