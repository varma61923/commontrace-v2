# CommonTrace

Persistent, governed memory for agents, with a randomized holdout that measures
whether the memory helps and withdraws memories that hurt.

Requires Python 3.10+. The core runs without an LLM.

```bash
python -m pip install -e .
commontrace up --dest /path/to/agent-project   # local gateway and console
commontrace --help                             # every command
python scripts/first_recall_smoke.py           # offline end-to-end check
```

## Layout

| Path | Contents |
|---|---|
| `commontrace/` | CLI, gateway, MCP server, stores, retrieval, typing, governance |
| `hub/` | Multi-tenant server (PostgreSQL, Alembic migrations) |
| `sdk/` | Generated clients: TypeScript, Go, Rust, JVM |
| `openapi/`, `hub/openapi.json` | HTTP contracts, checked by CI |
| `protocol/schemas/`, `protocol/conformance/` | JSON Schemas and conformance vectors |
| `deploy/`, `Dockerfile*`, `docker-compose.yml` | Helm, Terraform, Kubernetes, images |
| `distribution/` | Editor, marketplace and registry packaging |
| `benchmarks/`, `commons/eval/` | Evaluation harnesses (bring your own data) |
| `tests/`, `hub/tests/`, `e2e_tests/` | Test suites |

## Development

```bash
make lint      # ruff
make test      # tests and e2e_tests
make coverage  # with the 80% gate
```

AGPL-3.0-only. See [LICENSE](LICENSE).
