# Environment reference

Use `commontrace.yaml` for runtime options, model routes, budgets and server settings.
The recommended deployment surface has 25 environment variables for paths and credentials.
Explicit environment settings take precedence over YAML, without changing other requests.

`commontrace migrate config --dest . --dry-run` previews migration by name.
Omit `--dry-run` to atomically write nonsecret settings and credential references.
Credentials use `secrets: {COMMONTRACE_LLM_API_KEY: "env:MODEL_KEY"}`;
`file:` sources resolve against the store root and reread mounted secrets after rotation.
`aws:` sources use the existing bounded AWS Secrets Manager cache.

Legacy aliases remain supported for rolling upgrades. Their full, machine-readable inventory
and YAML paths are in [the configuration schema](../commontrace/configuration.schema.json)
under `x-legacy-environment-aliases`.
The schema has 155 alias entries; 24 also appear in the primary list.
That leaves 156 unique supported environment-variable names.
The fewer-than-40 public-variable target remains unmet while the full compatibility inventory is
supported and documented.

| Name | First source reference |
| --- | --- |
| `COMMONTRACE_CONFIG` | [commontrace/configuration.py:26](../commontrace/configuration.py#L26) |
| `COMMONTRACE_ROOT` | [commontrace/commands/_shellout.py:85](../commontrace/commands/_shellout.py#L85) |
| `COMMONTRACE_LLM_API_KEY` | [commontrace/commands/consolidate_cmd.py:66](../commontrace/commands/consolidate_cmd.py#L66) |
| `COMMONTRACE_HUB_API_KEY` | [commontrace/commands/_format.py:17](../commontrace/commands/_format.py#L17) |
| `COMMONTRACE_APPROVAL_KEY` | [commontrace/configuration.py:30](../commontrace/configuration.py#L30) |
| `COMMONTRACE_AGENT_TOKEN` | [commontrace/agent_registry.py:87](../commontrace/agent_registry.py#L87) |
| `COMMONTRACE_HANDOFF_KEY` | [commontrace/configuration.py:32](../commontrace/configuration.py#L32) |
| `COMMONTRACE_EMBED_API_KEY` | [commontrace/configuration.py:33](../commontrace/configuration.py#L33) |
| `COMMONTRACE_GEMINI_API_KEY` | [commontrace/configuration.py:34](../commontrace/configuration.py#L34) |
| `COMMONTRACE_COMMONS_VERIFY_KEY` | [commontrace/commons_integrity.py:90](../commontrace/commons_integrity.py#L90) |
| `HUB_DATABASE_URL` | [commontrace/configuration.py:36](../commontrace/configuration.py#L36) |
| `HUB_ADMIN_TOKEN` | [commontrace/configuration.py:37](../commontrace/configuration.py#L37) |
| `HUB_METRICS_TOKEN` | [commontrace/configuration.py:38](../commontrace/configuration.py#L38) |
| `HUB_API_KEY_PEPPER` | [commontrace/configuration.py:39](../commontrace/configuration.py#L39) |
| `HUB_CONSOLE_SECRET` | [commontrace/configuration.py:40](../commontrace/configuration.py#L40) |
| `HUB_ENCRYPTION_KEY` | [commontrace/configuration.py:41](../commontrace/configuration.py#L41) |
| `HUB_ENCRYPTION_KEY_PREVIOUS` | [commontrace/configuration.py:42](../commontrace/configuration.py#L42) |
| `HUB_COMMONS_SIGNING_KEY` | [commontrace/configuration.py:43](../commontrace/configuration.py#L43) |
| `HUB_STRIPE_SECRET_KEY` | [commontrace/configuration.py:44](../commontrace/configuration.py#L44) |
| `HUB_STRIPE_WEBHOOK_SECRET` | [commontrace/configuration.py:45](../commontrace/configuration.py#L45) |
| `HUB_KMS_KEY_ID` | [commontrace/configuration.py:46](../commontrace/configuration.py#L46) |
| `COMMONTRACE_NEO4J_PASSWORD` | [commontrace/configuration.py:47](../commontrace/configuration.py#L47) |
| `COMMONTRACE_FALKORDB_PASSWORD` | [commontrace/configuration.py:48](../commontrace/configuration.py#L48) |
| `HUB_CONNECTOR_SECRET` | [commontrace/configuration.py:49](../commontrace/configuration.py#L49) |
| `HUB_LEDGER_SIGNING_KEY` | [commontrace/configuration.py:50](../commontrace/configuration.py#L50) |
