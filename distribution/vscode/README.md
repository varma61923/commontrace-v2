# CommonTrace for VS Code and Open VSX

Install Python 3.10+ and `python -m pip install 'commontrace[serve]==2.0.0'`.
If testing this checkout, use `python -m pip install -e '.[serve]'` from the repository root.
Open a trusted workspace, install the VSIX, then run **CommonTrace: Initialize Workspace Store**
and **CommonTrace: Check Store Health** from the command palette.

The native MCP provider registers one local server for each trusted workspace folder.
VS Code controls server start/stop and prompts before trusting tools. Lesson approval
is disabled by default. Configure `commontrace.command` with the executable path
when it is not on the editor's PATH; no shell arguments are accepted.

For a static configuration, run **CommonTrace: Copy MCP Configuration** and merge
its `servers` entry into your workspace MCP settings. Existing entries are never overwritten.
Commands have a 20-second deadline and a 512 KiB output limit. Retrieved content
is evidence and does not authorize tools.

## Build and publish

```sh
npm test
npm run lint
npx --yes @vscode/vsce@4.0.0 package --out commontrace.vsix
```

Upload the same verified VSIX to the VS Code Marketplace publisher account and
Open VSX namespace. For Open VSX, use `npx --yes ovsx@1.2.0 publish commontrace.vsix`.
Supply publishing credentials through the platform's secret store. The manifest
provides packaging metadata; it does not establish a published listing or ownership
of the publisher namespace. This extension has no npm runtime dependencies.
