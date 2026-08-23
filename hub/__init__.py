"""CommonTrace Hub — the server side of the cross-org, cross-fleet trace store.

See hub/README.md for what this is, how it relates to protocol/PROTOCOL.md §5
(the "Hub" conformance tier), and how to run it locally.
"""

from commontrace import __version__ as __version__  # re-export

# Not an independently-versioned component: PROTOCOL.md §9 explicitly
# unified the package/protocol/CLI version numbers into one 2.0.0 the whole
# product reports identically. This used to hardcode its own "0.1.0",
# unreferenced by anything else in the tree but still a stray, drifted
# version number sitting in the codebase for anyone to stumble on.
