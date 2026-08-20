"""Read/write Markdown files with YAML frontmatter (the CommonTrace file format)."""
from __future__ import annotations

import contextlib
import os
import re
import tempfile
from typing import Any

import yaml

try:
    import fcntl
except ImportError:  # pragma: no cover -- POSIX-only stdlib module
    fcntl = None  # type: ignore[assignment]

# Delimiter must be its own line (optionally trailing whitespace / CR), not just the
# substring "---" anywhere in the file -- a plain `content.split("---", 2)` corrupts
# any field whose value happens to contain "---" (e.g. a title like "before---after").
# \r is allowed so a raw CRLF string parses even when it didn't come from a
# universal-newline text-mode read.
_DELIM_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)


class _StrictBoolLoader(yaml.SafeLoader):
    """SafeLoader, but a bare `yes`/`no`/`on`/`off` (any case) resolves as
    the plain string it looks like, not a bool.

    Every field this protocol declares as a string -- agent_type, domain,
    tags entries, a lesson's applies_when -- is one PyYAML's default
    resolver can silently misparse: `domain: NO` or `tags: [on, off]`
    become {'domain': False} / {'tags': [True, False]} with no error,
    because YAML 1.1 treats those tokens as booleans. `commontrace`'s own
    write path (yaml.safe_dump) auto-quotes them on output, so a
    round-tripped file is never at risk -- the exposure is a *hand-edited*
    file, which this protocol explicitly relies on (e.g. `lesson approve`/
    `reject` instruct manual edits).

    Rebuilds the resolver table on a SUBCLASS rather than mutating
    `yaml.SafeLoader.yaml_implicit_resolvers` in place: that dict is
    process-global, so patching it here would silently change bool
    resolution for every other `yaml.safe_load` call anywhere in the
    process (including third-party code), not just this module's reads.
    """

    yaml_implicit_resolvers = {
        first_char: [
            (tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"
        ]
        for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


_StrictBoolLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


class FrontmatterError(ValueError):
    """Raised when a file's frontmatter block is present but not parseable YAML,
    does not decode to a mapping, or the file cannot be opened at all.
    Callers should treat this as a clean, reportable error rather than letting
    a raw yaml.YAMLError/AttributeError/OSError traceback reach the user."""


def read(path: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body_markdown) for a `---\nYAML\n---\nbody` file."""
    # A path the user typed -- `lesson validate /nope/x.md`, or a directory
    # passed where a file was meant -- is a user error, not a crash. Every
    # other error path in this CLI prints "[commontrace] ..." and exits
    # non-zero; letting a raw FileNotFoundError/IsADirectoryError through
    # made this the odd one out.
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            content = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise FrontmatterError(f"cannot read {path}: {exc}") from exc
    if not content.startswith("---"):
        return {}, content
    delims = list(_DELIM_RE.finditer(content))
    if len(delims) < 2:
        return {}, content
    fm_text = content[delims[0].end():delims[1].start()]
    try:
        fm = yaml.load(fm_text, Loader=_StrictBoolLoader)
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"{path}: malformed YAML frontmatter: {exc}") from exc
    if fm is None:
        fm = {}
    if not isinstance(fm, dict):
        raise FrontmatterError(
            f"{path}: frontmatter must be a YAML mapping, got {type(fm).__name__}"
        )
    body = content[delims[1].end():].lstrip("\n")
    return fm, body


def write(path: str, frontmatter: dict[str, Any], body: str) -> None:
    fm_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    target_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(target_dir, exist_ok=True)
    temp_file = tempfile.NamedTemporaryFile(
        dir=target_dir,
        delete=False,
        mode="w",
        encoding="utf-8",
        newline="\n",
    )
    temp_path = temp_file.name
    try:
        with temp_file as fh:
            fh.write("---\n")
            fh.write(fm_text)
            fh.write("---\n\n")
            fh.write(body.rstrip("\n") + "\n")
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise


@contextlib.contextmanager
def locked(path: str):
    """Serialize a read-modify-write critical section against `path`
    across processes: `with frontmatter.locked(path): fm, body =
    frontmatter.read(path); ...; frontmatter.write(path, fm, body)`.

    write()'s NamedTemporaryFile + os.replace() makes one call to write()
    atomic -- readers never see a half-written file -- but atomicity of a
    single write is not the same as safety for two concurrent
    read-modify-writers. If writer A and writer B each independently read
    the file, change a DIFFERENT field, and write back, both writes are
    individually atomic and neither corrupts the file -- but B's
    os.replace() completes after A's, so B's write silently overwrites
    A's change with the pre-A content B started from. No error, no
    exception, no corrupted file -- just a logical update quietly lost.
    Reproduced directly: two OS processes racing an approve-style
    read-modify-write on the same lesson file lost one of the two changes
    in 5/5 runs.

    Locks a dedicated, stable sibling file (`path + ".lock"`), NOT `path`
    itself. Locking `path` directly would defeat itself against write()'s
    own atomic-replace design: os.replace() swaps in a new inode, so a
    second writer that opened `path` (and is blocked waiting for the
    lock) is blocked on the OLD inode: once unblocked it reads through
    its already-open file descriptor, seeing the pre-replace content, not
    what the first writer just wrote. A lock file that no writer ever
    replaces keeps the same inode across every acquisition, so "holds the
    lock" and "sees the latest content" never drift apart -- this is
    exactly the "synchronize on a shared lock identity, not independent
    temporary files" fix, as opposed to each writer locking its own
    NamedTemporaryFile (which never contends with anyone).

    POSIX only (fcntl.flock). On a platform without fcntl (e.g. native
    Windows, not WSL) this degrades to no synchronization at all rather
    than raising -- consistent with this module's existing policy that a
    robustness feature must never be the reason a capture/approve/write
    fails outright -- but that means the race this function exists to
    close is NOT closed there. This is a known, real limitation, not a
    claim of full cross-platform correctness.

    Does not address: a process crashing while holding the lock (the OS
    releases flock automatically when the holding process's file
    descriptors close, including on crash, so a stale lock cannot outlive
    its process); NFS or other network filesystems, where flock semantics
    are unreliable or unsupported -- this is designed for the local
    filesystem `memory/` is expected to live on.
    """
    if fcntl is None:
        yield
        return

    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
