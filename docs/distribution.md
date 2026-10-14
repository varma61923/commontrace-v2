# CommonTrace distributions

The CLI and Hub are Python distributions. Install the CLI on Python
3.10 or newer with `python -m pip install 'commontrace[serve]==2.0.0'`; use
`python -m pip install -e '.[serve]'` from a checkout. `commontrace doctor` checks
the installation. The existing root [`LICENSE`](../LICENSE) is GNU AGPL version 3
only; package, API and marketplace metadata use `AGPL-3.0-only`.

## MCP clients and registries

Use `commontrace integration --client <client> --dest <store>` to print a valid
configuration fragment without changing editor settings. Supported targets are
`claude-desktop`, `cursor`, `windsurf`, `zed`, `vscode` and `jetbrains`. The server
uses local stdio by default; remote HTTP requires deliberate operator configuration.
Lesson approval tools stay disabled unless `--approval` is supplied. Merge the
fragment into an existing client configuration instead of replacing unrelated
entries. Credentials are never generated or inserted into MCP config.

The [Smithery manifest](../smithery.yaml) exposes the same local stdio server.
It requires an absolute persistent `storeRoot` and keeps lesson approval disabled
by default. The existing [MCP registry package](../distribution/mcp-registry/server.json)
is the PyPI distribution declaration; registry ownership/publication requires the
maintainer's account and is not implied by a valid manifest.

The existing [Claude marketplace plugin](../distribution/claude-marketplace/README.md)
continues to carry its skill and MCP config. Desktop/local server installation and
the hosted Hub service remain distinct deployments.

## VS Code and Open VSX

See the [extension guide](../distribution/vscode/README.md). Install the VSIX into
VS Code or Open VSX, trust the workspace, initialize its store, and start the
registered MCP server through the editor. The extension ships zero npm runtime
dependencies. CI tests workspace isolation, trust behavior, bounded child-process
execution, and packages a VSIX artifact. An Open VSX namespace/publisher and access
token are required for an actual registry upload.

## JetBrains Marketplace

See the [JetBrains plugin guide](../distribution/jetbrains/README.md). Install the
built ZIP from disk or upload it through JetBrains Marketplace. The plugin executes
the installed CLI in a background task, refuses untrusted projects, asks before
initializing persistent data, and uses local MCP configuration for AI Assistant.
CI builds and retains the ZIP; marketplace credentials and signing remain with the
publisher.

## GitHub Marketplace

The root [action.yml](../action.yml) packages CommonTrace itself and checks a local
store without sending it to a hosted service. Example:

```yaml
- uses: actions/checkout@v4
- uses: varma61923/commontrace-v2@<reviewed-release-tag>
  with:
    store: .
    initialize: 'false'
```

Choose a reviewed immutable action commit in production. The action confines its
store to the checked-out workspace, validates the initialization flag, and invokes
the installed CLI with an argument array. CI runs the action on a disposable store.
The action is built from the repository root; the `<reviewed-release-tag>` example must be replaced with a signed-off release tag. Marketplace visibility requires an owner-managed listing.

## OpenAPI and SDKs

The live CLI gateway, full CLI gateway surface, and hosted Hub REST API each have a
standard OpenAPI document. Export them with `python scripts/export_openapi.py` and
`python scripts/export_hub_openapi.py`; the CI checks reject stale generated
contracts. Install the Hub build dependencies with `python -m pip install -r hub/requirements.txt`
before exporting its contract. Validate and generate language clients with the SHA256-pinned upstream
Swagger-compatible OpenAPI Generator:

```sh
python scripts/generate_sdks.py --api memory --languages typescript go rust java kotlin
python scripts/generate_sdks.py --api gateway --languages typescript go rust java kotlin
python scripts/generate_sdks.py --api hub --languages typescript go rust java kotlin
```

Generated code goes into fresh ignored output directories. CI compiles every API
contract in all five languages and retains the generated source artifacts; it does
not hand-author SDKs or publish package releases. Publish a reviewed version through
the npm, Go, crates.io or Maven publisher accounts when those accounts and
credentials are configured. MCP's `tools/list` JSON-RPC protocol remains described
by the MCP server itself rather than being recast as an HTTP API.
