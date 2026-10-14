# Gemini MCP integration

Gemini CLI and MCP-capable Gemini hosts can connect to the same hosted Hub over
Streamable HTTP. For a team-operated Gemini CLI, add a remote server entry to
the Gemini MCP configuration and supply the per-user OIDC access token through
its supported secret/environment-variable mechanism:

```json
{
  "mcpServers": {
    "commontrace": {
      "httpUrl": "https://memory.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${COMMONTRACE_OIDC_ACCESS_TOKEN}"
      }
    }
  }
}
```

Configure `HUB_OIDC_ISSUER`, `HUB_OIDC_AUDIENCE` (the full MCP resource URI),
and `HUB_OIDC_JWKS_URI` or `HUB_OIDC_JWKS` on the Hub. Provision and link each
user explicitly with `hub.manage link-sso`. The identity provider owns the
interactive login and token refresh; do not put a long-lived user token in a
checked-in configuration file. Use a managed credential helper or short-lived
environment injection and rotate/revoke tokens through the IdP.

For a public Gemini extension or marketplace submission, use the same endpoint
and OAuth setup, then add the platform's required listing metadata, privacy
policy, and support contact. The remote MCP service advertises RFC 9728 metadata
for hosts that implement OAuth discovery. Test with Viewer and Curator users to
confirm the Hub role gates both reads and writes.

This is an MCP connection guide, not a claim that a public Gemini marketplace
listing has been approved. See [distribution setup](../../docs/distribution.md).
