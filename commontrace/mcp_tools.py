"""The local MCP server's tool surface, as names.

Deliberately dependency-free -- this module imports nothing, and that is its
entire reason to exist as a separate file.

`commontrace install` writes the tool list into the MCP config it generates,
so it needs these names. It used to get them by importing
`commontrace.mcp_server`, which pulls in the whole retrieval stack
(`evidence_io` -> `frontmatter` -> `yaml`) to read a tuple of strings. That
made `install_cmd` unimportable anywhere PyYAML is absent -- which is not a
hypothetical environment, it is the one the Hub's own test job runs in, and
it broke `hub/tests/test_install_template_surface.py` on every Python
version at once.

Splitting the names out keeps the heavy import where it belongs: in the code
that actually serves the tools. `mcp_server` re-exports these, so
`mcp_server.LOCAL_TOOLS` still resolves for every existing caller, and
tests/test_mcp_server.py asserts the list matches what the built server
really registers.
"""

from __future__ import annotations

# Every tool `commontrace serve` exposes. `commontrace install` advertises
# this list in the generated config WITHOUT importing the MCP SDK, because
# the client package ships with PyYAML alone and importing the SDK there
# would make `commontrace install` fail on exactly the machines it is meant
# to set up.
LOCAL_TOOLS = (
    "retrieve", "capture", "propose_lessons", "list_lessons", "get_lesson",
    "draft_lesson", "approve_lesson", "reject_lesson", "store_status",
    "experiment_status",
)

# Removed by `serve --no-approval`.
APPROVAL_TOOLS = ("approve_lesson", "reject_lesson")
