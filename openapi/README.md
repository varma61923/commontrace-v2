# OpenAPI contracts

Every HTTP API CommonTrace serves is described here, generated from the live route tables and checked in CI.

| Document | Covers | Served at | Regenerate / check |
|---|---|---|---|
| [`gateway.json`](gateway.json) (OpenAPI 3.0.3) | All 58 gateway operations: memory, governed control, agents, recall/outcome/episode, conversations, lesson review, Learning Ledger, marketplace, health | `GET /v1/openapi.json`, Swagger UI at `/v1/docs` | `python scripts/export_openapi.py [--check]` |
| [`../sdk/openapi.json`](../sdk/openapi.json) (OpenAPI 3.0.3) | The memory subset the generated SDKs are built from | — | same command |
| [`../hub/openapi.json`](../hub/openapi.json) (OpenAPI 3.1.0) | All 106 Hub operations: MCP, REST, marketplace, telemetry, OTLP, connectors, billing webhook, SCIM, console, admin, health | `GET /api/v1/openapi.json` | `python scripts/export_hub_openapi.py [--check]` |

Both generators refuse to build when a live route has no contract entry, or an entry names a route that no
longer exists. `tests/test_gateway_openapi_contract.py` calls every gateway route against a seeded store and
validates each live request and response against its declared schema; `hub/tests/test_openapi.py` checks
coverage, parameters, security and validity for the Hub. MCP tool arguments are not HTTP routes; clients
discover them with `tools/list`.
