# Environment reference

Use `commontrace.yaml` profiles for runtime options, model routes, budgets, server settings and secrets.
The first-class environment override surface documents 25 names for paths, provider selection and credentials.
Explicit environment settings take precedence over YAML during rolling upgrades.
Choose a profile in `commontrace.yaml`; settings can be tailored per local, team or server deployment:

```yaml
profile: local
profiles:
  local:
    offline: true
    llm_provider: local
    settings:
      llm:
        max_tokens: 240
  team:
    llm_provider: openai
    llm_model: your-model
    secrets:
      COMMONTRACE_LLM_API_KEY: env:TEAM_MODEL_KEY
  server:
    settings:
      server:
        host: 0.0.0.0
        port: 8765
```

`commontrace migrate config --dest . --dry-run` previews migration by name.
Omit `--dry-run` to atomically write nonsecret settings and credential references.
Credentials use `secrets: {COMMONTRACE_LLM_API_KEY: "env:MODEL_KEY"}`;
`file:` sources resolve against the store root and reread mounted secrets after rotation.
`aws:` sources use the existing bounded AWS Secrets Manager cache.

Profile secrets override top-level secret references; explicit environment values still take precedence.
This lets each profile select its own mounted credential while keeping environment migration compatible:

```yaml
profile: team
profiles:
  team:
    secrets:
      COMMONTRACE_LLM_API_KEY: file:keys/team-llm
```

Compatibility is retained: `commontrace migrate config` recognizes the complete legacy alias
inventory and its YAML paths in [the configuration schema](../commontrace/configuration.schema.json)
under `x-legacy-environment-aliases`.
The schema has 155 alias entries; 24 also appear in the first-class list.
These aliases remain supported for upgrades; 156 unique names are accepted
when the first-class environment list is combined with the legacy aliases.
Use profiles for new deployments and migrate legacy settings gradually; migration does not delete
the original environment values.

| First-class environment override | First source reference |
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
