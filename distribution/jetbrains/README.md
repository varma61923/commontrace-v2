# CommonTrace for JetBrains IDEs

This plugin adds three Tools menu actions: initialize the project store, check its
health, and copy local MCP configuration for AI Assistant. It uses the installed CLI
in a background task, without invoking a shell. Untrusted projects cannot run it.

Install `commontrace[serve]==2.0.0` into a Python 3.10+ environment, or install this
checkout with `python -m pip install -e '.[serve]'`. Start the IDE with that executable
on PATH, or set `COMMONTRACE_EXECUTABLE` to its absolute path in the IDE environment.
Initialize a store, copy MCP configuration, and merge it into AI Assistant's MCP
settings. Approval tools are disabled. Existing assistant settings are never changed.

Java 21 and Gradle 8.14.3 build the plugin:

```sh
gradle --no-daemon test buildPlugin
```

Install the ZIP from `build/distributions` using **Plugins → Install Plugin from Disk**.
CI builds and retains the ZIP. Upload a verified ZIP through the JetBrains Marketplace
publisher account; configure signing and upload credentials in the publisher's secret
store. There are no embedded publishing credentials or automatic uploads.
