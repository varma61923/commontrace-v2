# WS7: Adoption surface

**Problem.** Time to first useful recall must be under a minute for every common runtime.

**Design.** `pip install`, generated SDKs (TypeScript, Go, Rust, Java, Kotlin) from the OpenAPI memory subset, agent self-signup, MCP over stdio/HTTP with OAuth 2.1, a local container (`Dockerfile.local`), and typed OpenAPI for every HTTP API (`openapi/`).

**Invariants touched.** SDKs are generated from `sdk/openapi.json`, never hand-edited; both OpenAPI generators refuse undocumented routes.

**Tests.** `tests/test_gateway_openapi_contract.py` (every route's live response validated), `hub/tests/test_openapi.py`, `tests/test_local_image.py`, SDK generation in CI.

**Benchmark to move.** Target G (< 60 s to first recall); nothing is published to registries yet.

**Risks.** Registry publishing needs credentials outside this repository.

**Rollback.** Releases are tagged; the previous package version stays installable.
