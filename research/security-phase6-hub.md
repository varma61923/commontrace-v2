# Hub security review — phase 6

Reviewed against baseline `c8ce5c792916c10e23b0618c59259960294b7bcc` on
`feat/official-benchmark-judges`. Scope: Hub authentication, regional tenant
boundaries, REST/OTLP ingestion, webhook egress, query construction, and caches.
No external inference, competitor code, dependency additions, deployments, or
changes to `memory/`.

## Confirmed findings and fixes

1. **OIDC bypassed regional residency enforcement.** API keys call `_region_ok`,
   but a valid linked OIDC user previously authenticated on a deployment serving
   a different region from their organization. `verify_user_token` now applies
   the same regional check before updating login state. Regression tests sign
   real RS256 tokens and compare user/API-key acceptance for matching, mismatched,
   and unpinned organizations. A rejected regional login leaves `last_login_at`
   unchanged.

2. **Webhook recipients controlled unbounded response buffering and amplified
   delivery latency.** `client.post` read the recipient's entire response even
   though delivery uses only its status. A receiver could stream arbitrarily
   much data or send a highly compressed body. Individual HTTPX socket timeouts
   also excluded DNS and restarted on pinned address fallbacks. Delivery now
   streams only headers, closes the response without reading its body, and uses
   one overall deadline covering DNS, connection fallbacks, request transmission,
   and response headers. Existing DNS pinning, private-address rejection, TLS
   hostname verification, no redirects, and no environment proxy remain intact.
   Tests exercise success, redirect, error, stuck DNS, and many slow addresses.

3. **REST/OTLP sessions omitted their configured RLS backstop.** Their
   authenticated data operations did not set `auth.current_org_id`; therefore
   `session_scope` never set the transaction-local `app.org_id` GUC. The installed
   policies intentionally allow unscoped operator work, so these requests also
   took that bypass. This is a missing defense layer, **not a demonstrated
   cross-tenant data leak**: reviewed CRUD queries still had explicit tenant
   predicates. `session_scope(..., org_id=authenticated.org_id)` now binds these
   transactions and their nested sessions to the authenticated tenant. Existing
   operator/background calls keep their intentional context behavior. Tests
   inspect the real Postgres GUC during concurrent requests for different orgs
   on REST write, REST search, and OTLP write. Additional checks cover nested
   sessions, connection reuse, transaction rollback, cancellation, and context
   restoration.

## Local measurements

`python research/security-phase6-webhook-profile.py` compares the original
response-handling code to the fixed transport against a simulated 64 MiB
recipient response, without opening sockets. Three measured samples follow one
warmup for each path; Python allocations use `tracemalloc`.

| Path | Median duration | Median peak Python allocation | Response consumed |
| --- | ---: | ---: | ---: |
| Buffered baseline | 29.192 ms | 65.016 MiB | 64 MiB |
| Fixed streaming | 5.922 ms | 0.014 MiB | 0 MiB |

This measures avoidable client allocation/processing, not network throughput or
production scheduler throughput. One hostile recipient no longer requires its
body to end before delivery succeeds. Cancelling DNS bounds the coroutine's
wait; an already-running system resolver thread cannot be forcibly stopped.

## Verification

Disposable Postgres 16, local port 55432:

```sh
HUB_TEST_DATABASE_URL=postgresql+asyncpg://commontrace_dev:devpassword@localhost:55432/commontrace_hub_test \
  python -m pytest hub/tests/test_security_phase6_hub.py hub/tests/test_events.py \
  hub/tests/test_auth.py hub/tests/test_user_identity.py hub/tests/test_rest.py \
  hub/tests/test_otlp.py hub/tests/test_auth_cache.py hub/tests/test_tenant_isolation.py \
  hub/tests/test_cross_tenant_fuzz.py -q
```

**215 passed in 51.50 seconds**, including 13 new security regressions. Targeted
Ruff and diff whitespace checks passed. No further Hub production-source changes
are pending from this review.

No additional exploitable issue was confirmed in reviewed SQL term escaping,
tenant-filtered trace reads/deletions, connector signature/tenant binding, or
bounded authentication cache. Those paths were left unchanged.
