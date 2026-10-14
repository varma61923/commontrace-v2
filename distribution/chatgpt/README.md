# ChatGPT and OpenAI app directory

CommonTrace Hub can be connected as a remote MCP app from ChatGPT's developer
mode and packaged for an owner-managed app-directory submission.

## Configure the Hub

1. Deploy `hub.server:build_server_app` at a stable HTTPS URL and keep its
   Streamable HTTP endpoint reachable at `/mcp` (or configure
   `HUB_STREAMABLE_HTTP_PATH`).
2. Configure `HUB_OIDC_ISSUER`, `HUB_OIDC_AUDIENCE` and either
   `HUB_OIDC_JWKS_URI` or `HUB_OIDC_JWKS`. The audience must be the public MCP
   resource URI, such as `https://memory.example.com/mcp`.
3. Create a Hub user, assign the minimum role required, and link the exact
   OIDC `(issuer, subject)` with `hub.manage link-sso`.
4. Confirm that an unauthenticated `GET /mcp` returns a 401 with
   `WWW-Authenticate: Bearer resource_metadata=...`, and that the discovered
   `/.well-known/oauth-protected-resource/mcp` document names the right
   audience and issuer.

The identity provider performs interactive sign-in and token issuance. It must
support the OAuth flow required by the client (authorization code with PKCE) and
issue a token whose audience exactly matches `HUB_OIDC_AUDIENCE`. CommonTrace
does not store the provider's client secret or issue user tokens.

## Connect and submit

In ChatGPT developer mode, create an MCP app/connector, provide the HTTPS MCP
endpoint, and choose OAuth when prompted. The resource metadata points ChatGPT
to the configured identity provider. Verify sign-in with a linked user, then
test read and write tools with separate Viewer and Curator accounts before
submitting the listing. Keep the listing's privacy policy, support contact,
data-retention behavior, and tool descriptions aligned with the deployment.

Use the [distribution guide](../../docs/distribution.md) for deployment and
security details. Directory publication, OAuth client registration at the IdP,
and app review require the publisher's own accounts and approval.
